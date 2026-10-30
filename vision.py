"""Image attachments: validate, download and convert to ``image_url`` parts."""
from __future__ import annotations

import base64
import logging
from typing import Iterable

import discord
import httpx

import config

log = logging.getLogger("rukia.vision")

_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp")


class VisionError(Exception):
    """A problem with the user's attachments, with a friendly message."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _sniff_mime(data: bytes) -> str | None:
    """Identify the real image type from its first bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _looks_like_image(att: discord.Attachment) -> bool:
    ctype = (att.content_type or "").split(";")[0].strip().lower()
    return ctype in _ALLOWED_TYPES or att.filename.lower().endswith(_ALLOWED_EXT)


async def _download(client: httpx.AsyncClient, att: discord.Attachment) -> bytes:
    """Download one attachment, refusing anything over the size limit."""
    limit = config.MAX_IMAGE_BYTES
    data = bytearray()
    async with client.stream("GET", att.url) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise VisionError(f"`{att.filename}` is too big. Images must be under 8 MB.")
    return bytes(data)


async def build_image_parts(attachments: Iterable[discord.Attachment]) -> list[dict]:
    """Validate attachments and return OpenAI ``image_url`` content parts.

    Raises VisionError (with a polite message) for unsupported files, too many
    images or files that are too large.
    """
    atts = list(attachments)
    bad = [a for a in atts if not _looks_like_image(a)]
    if bad:
        names = ", ".join(f"`{a.filename}`" for a in bad[:3])
        raise VisionError(
            f"Sorry, I can only read PNG, JPG or WEBP images, not {names}. "
            "Please attach a photo or screenshot instead."
        )
    if len(atts) > config.MAX_IMAGES:
        raise VisionError(f"Please send at most {config.MAX_IMAGES} images at a time.")
    for a in atts:
        if a.size > config.MAX_IMAGE_BYTES:
            raise VisionError(f"`{a.filename}` is too big. Images must be under 8 MB.")

    parts: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for a in atts:
                data = await _download(client, a)
                mime = _sniff_mime(data)
                if mime is None:
                    raise VisionError(f"`{a.filename}` does not look like a PNG, JPG or WEBP image.")
                b64 = base64.b64encode(data).decode("ascii")
                parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    except VisionError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Image download failed: %s", exc)
        raise VisionError("I couldn't download that image. Please try attaching it again.") from exc
    return parts
