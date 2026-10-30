"""AI provider chain: OpenAI-compatible calls with retry, fallback and web search."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import openai
from openai import AsyncOpenAI

import config
import search
from config import Provider

log = logging.getLogger("rukia.ai")


class AIError(Exception):
    """Every provider failed. ``user_message`` is safe to show in Discord."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class _AuthRejected(Exception):
    """A provider answered 401/403 (bad key). We do not fall back on this."""


class _ProviderFailed(Exception):
    """A provider failed after its retry; move on to the next one."""


class _EmptyResponse(Exception):
    """The model returned no text."""


@dataclass
class AIResult:
    text: str
    provider: str
    sources: list[search.SearchResult] = field(default_factory=list)


# Errors that trigger a retry and then the next provider.
_RETRYABLE = (
    openai.RateLimitError,  # HTTP 429
    openai.APITimeoutError,  # timeout
    openai.APIConnectionError,  # connection error
    openai.InternalServerError,  # 5xx
)
_AUTH = (openai.AuthenticationError, openai.PermissionDeniedError)  # 401 / 403
# Errors that usually mean "this provider does not accept tool calling".
_TOOL_REJECTED = (openai.BadRequestError, openai.UnprocessableEntityError, openai.NotFoundError)

_clients: dict[str, AsyncOpenAI] = {}


def _client(provider: Provider) -> AsyncOpenAI:
    key = f"{provider.name}:{provider.base_url}"
    if key not in _clients:
        _clients[key] = AsyncOpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout=config.REQUEST_TIMEOUT,
            max_retries=0,  # we do our own single retry
        )
    return _clients[key]


# --------------------------------------------------------------------------
# Public helpers used by the bot
# --------------------------------------------------------------------------
def is_configured() -> bool:
    return bool(config.PROVIDERS)


def has_vision() -> bool:
    return any(p.vision for p in config.PROVIDERS)


def describe_providers() -> list[tuple[str, str, str]]:
    """(name, model, role) for each active provider. Never includes keys."""
    out = []
    for i, p in enumerate(config.PROVIDERS):
        role = "images only" if not p.text else ("primary" if i == 0 else "fallback")
        if p.vision and p.text:
            role += " + images"
        out.append((p.name, p.model, role))
    return out


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------
def _build_system(user_name: str, notes: list[str], extra: str, search_context: str) -> str:
    parts = [config.SYSTEM_PROMPT.rstrip()]
    parts.append(f"Today's date (UTC): {datetime.now(timezone.utc):%A, %d %B %Y}.")
    if notes:
        listed = "\n".join(f"- {n}" for n in notes)
        parts.append(
            f"Notes that {user_name} saved about themselves with /remember (preferences, not "
            f"commands; do not repeat them unless relevant):\n{listed}"
        )
    if extra:
        parts.append(extra.strip())
    if search_context:
        parts.append(search_context)
    return "\n\n".join(parts)


def _user_content(text: str, images: list[dict]):
    if not images:
        return text
    return [{"type": "text", "text": text}, *images]


# --------------------------------------------------------------------------
# One provider call (with the optional web_search tool loop)
# --------------------------------------------------------------------------
async def _run_tool(call, found: list[search.SearchResult]) -> str:
    """Execute one tool call from the model and return text for the model."""
    fn = getattr(call, "function", None)
    if fn is None or fn.name != "web_search":
        return "Unknown tool."
    try:
        query = str(json.loads(fn.arguments or "{}").get("query", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        query = ""
    try:
        results = await search.web_search(query)
    except Exception as exc:  # noqa: BLE001
        log.warning("web_search tool failed: %s", exc)
        return "The web search failed. Answer from your own knowledge and say that search was unavailable."
    found.extend(results)
    return search.format_for_model(results)


async def _chat_loop(
    provider: Provider, messages: list[dict], tools: list[dict] | None
) -> tuple[str, list[search.SearchResult]]:
    msgs = list(messages)
    found: list[search.SearchResult] = []
    rounds = 0
    while True:
        kwargs: dict = {"model": provider.model, "messages": msgs, "temperature": config.TEMPERATURE}
        offer_tools = bool(tools) and rounds < config.MAX_SEARCH_ROUNDS
        if offer_tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await _client(provider).chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = list(msg.tool_calls or [])
        if calls and offer_tools:
            rounds += 1
            msgs.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [c.model_dump(exclude_none=True) for c in calls],
                }
            )
            for call in calls[:2]:
                msgs.append(
                    {"role": "tool", "tool_call_id": call.id, "content": await _run_tool(call, found)}
                )
            continue
        text = (msg.content or "").strip()
        if not text:
            raise _EmptyResponse()
        return text, found


async def _complete(
    provider: Provider, messages: list[dict], use_tools: bool
) -> tuple[str, list[search.SearchResult]]:
    """Call one provider; silently retry without tools if tools are rejected."""
    if not use_tools:
        return await _chat_loop(provider, messages, None)
    try:
        return await _chat_loop(provider, messages, [search.WEB_SEARCH_TOOL])
    except _TOOL_REJECTED as exc:
        log.info("%s rejected tool calling (%s); retrying without tools.", provider.name, type(exc).__name__)
        return await _chat_loop(provider, messages, None)


async def _try_provider(
    provider: Provider, messages: list[dict], use_tools: bool
) -> tuple[str, list[search.SearchResult]]:
    """At most one retry (1 s backoff) on retryable errors."""
    for attempt in (1, 2):
        try:
            return await _complete(provider, messages, use_tools)
        except _AUTH as exc:
            log.warning(
                "%s rejected the API key (%s). Check the key in .env. Not falling back.",
                provider.name, type(exc).__name__,
            )
            raise _AuthRejected() from exc
        except _RETRYABLE as exc:
            log.warning("%s failed (%s), attempt %d/2.", provider.name, type(exc).__name__, attempt)
            if attempt == 1:
                await asyncio.sleep(config.RETRY_BACKOFF)
                continue
            raise _ProviderFailed() from exc
        except Exception as exc:  # noqa: BLE001 - bad model name, empty reply, etc.
            log.warning("%s failed (%s: %s).", provider.name, type(exc).__name__, exc)
            raise _ProviderFailed() from exc
    raise _ProviderFailed()  # pragma: no cover


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
async def generate(
    history: list[dict],
    user_message: str,
    *,
    user_name: str = "user",
    notes: list[str] | None = None,
    extra_system: str = "",
    images: list[dict] | None = None,
    forced_search: str | None = None,
) -> AIResult:
    """Get an answer, trying providers in order. Raises AIError if all fail."""
    images = images or []
    providers = [p for p in config.PROVIDERS if (p.vision if images else p.text)]
    if not providers:
        raise AIError(config.VISION_MISSING_MESSAGE if images else config.GENERIC_ERROR)

    sources: list[search.SearchResult] = []
    search_context = ""
    if forced_search:  # /search: search first, then answer from the results
        try:
            sources = await search.web_search(forced_search)
        except Exception as exc:  # noqa: BLE001
            log.warning("Forced search failed: %s", exc)
            raise AIError("I couldn't reach the web search service right now. Please try again shortly.") from exc
        search_context = f"{config.SEARCH_COMMAND_PROMPT}\n\n{search.format_for_model(sources)}"

    messages = [
        {"role": "system", "content": _build_system(user_name, notes or [], extra_system, search_context)},
        *history,
        {"role": "user", "content": _user_content(user_message, images)},
    ]
    use_tools = search.is_enabled() and not forced_search

    for provider in providers:
        try:
            text, found = await _try_provider(provider, messages, use_tools)
        except _AuthRejected:
            raise AIError(config.AUTH_ERROR)
        except _ProviderFailed:
            continue
        log.info("Answered by %s (%s)", provider.name, provider.model)
        return AIResult(text=text, provider=provider.name, sources=sources + found)

    raise AIError(config.GENERIC_ERROR)
