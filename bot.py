"""Rukia: a Discord AI assistant bot. Entry point: ``python bot.py``."""
import logging
import os
import re
import sys
from typing import Optional

from aiohttp import web

import discord
from discord import app_commands

import ai
import config
import search
import vision
from db import Database, create_database
from memory import ConversationMemory
from utils import split_message

log = logging.getLogger("rukia")


class RukiaBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # needed for mentions and DMs
        super().__init__(
            intents=intents,
            # The model must never be able to ping @everyone/@here/users.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = app_commands.CommandTree(self)
        self.db: Optional[Database] = None
        self.memory = ConversationMemory(None)

    async def _start_health_server(self) -> None:
        port_str = os.getenv("PORT")
        if not port_str:
            return
        try:
            port = int(port_str)
            app = web.Application()
            app.router.add_get("/", lambda req: web.Response(text="Rukia is running!"))
            app.router.add_get("/health", lambda req: web.Response(text="OK", status=200))
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            log.info("Health server listening on port %d", port)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not start health server: %s", exc)

    async def setup_hook(self) -> None:
        await self._start_health_server()
        self.db = await create_database(config.DATABASE_URL)
        self.memory = ConversationMemory(self.db)
        synced = await self.tree.sync()
        log.info("Synced %d slash commands.", len(synced))

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Rukia is online as %s (id %s).", self.user, self.user.id if self.user else "?")


bot = RukiaBot()


# --------------------------------------------------------------------------
# Core helpers
# --------------------------------------------------------------------------
def _clean_name(name: str) -> str:
    """Make a display name safe for the 'username: text' format."""
    return re.sub(r"[\r\n:]+", " ", name).strip()[:32] or "user"


async def _load_notes(user_id: int) -> list[str]:
    if bot.db is None:
        return []
    try:
        return await bot.db.get_notes(user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load notes (%s).", exc)
        return []


async def generate_reply(
    *,
    channel_id: int,
    user: discord.abc.User,
    text: str,
    attachments: tuple = (),
    extra_system: str = "",
    forced_search: Optional[str] = None,
) -> list[str]:
    """Run one question through memory + AI and return Discord-sized chunks."""
    name = _clean_name(user.display_name)

    images: list[dict] = []
    if attachments:
        if not ai.has_vision():
            return [config.VISION_MISSING_MESSAGE]
        try:
            images = await vision.build_image_parts(attachments)
        except vision.VisionError as exc:
            return [exc.user_message]

    # Only a text note about images is ever stored, never the image data.
    note = ""
    if images:
        note = f" [user attached {len(images)} image{'s' if len(images) != 1 else ''}]"

    async with bot.memory.lock(channel_id):
        history = await bot.memory.history(channel_id)
        notes = await _load_notes(user.id)
        try:
            result = await ai.generate(
                history,
                f"{name}: {text}{note}",
                user_name=name,
                notes=notes,
                extra_system=extra_system,
                images=images,
                forced_search=forced_search,
            )
        except ai.AIError as exc:
            return [exc.user_message]
        except Exception:  # noqa: BLE001 - never crash on an AI problem
            log.exception("Unexpected error while generating a reply")
            return [config.GENERIC_ERROR]
        # Only successful exchanges are stored.
        await bot.memory.add_exchange(channel_id, f"{name}: {text}{note}", result.text)

    reply = result.text + search.sources_footer(result.sources)
    return split_message(reply) or [config.GENERIC_ERROR]


async def _slash_chat(
    interaction: discord.Interaction,
    text: str,
    *,
    image: Optional[discord.Attachment] = None,
    extra_system: str = "",
    forced_search: Optional[str] = None,
) -> None:
    """Shared body of /ask, /solve, /explain and /search."""
    await interaction.response.defer(thinking=True)  # AI calls take > 3 seconds
    channel_id = interaction.channel_id or interaction.user.id
    try:
        chunks = await generate_reply(
            channel_id=channel_id,
            user=interaction.user,
            text=text,
            attachments=(image,) if image else (),
            extra_system=extra_system,
            forced_search=forced_search,
        )
    except Exception:  # noqa: BLE001
        log.exception("Slash command failed")
        chunks = [config.GENERIC_ERROR]
    for chunk in chunks:
        await interaction.followup.send(chunk)


# --------------------------------------------------------------------------
# Mentions and DMs
# --------------------------------------------------------------------------
@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or bot.user is None:
        return
    is_dm = message.guild is None
    if not (is_dm or bot.user in message.mentions):
        return

    text = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()
    attachments = tuple(message.attachments)
    if not text and not attachments:
        await message.reply(
            f"Hi {message.author.display_name}! What would you like to ask me?",
            mention_author=False,
        )
        return
    if not text:
        text = "Please look at the attached image and help me with it."

    try:
        async with message.channel.typing():
            chunks = await generate_reply(
                channel_id=message.channel.id,
                user=message.author,
                text=text,
                attachments=attachments,
            )
    except Exception:  # noqa: BLE001
        log.exception("on_message failed")
        chunks = [config.GENERIC_ERROR]

    try:
        await message.reply(chunks[0], mention_author=False)
    except discord.HTTPException:  # e.g. original message was deleted
        await message.channel.send(chunks[0])
    for chunk in chunks[1:]:
        await message.channel.send(chunk)


# --------------------------------------------------------------------------
# Slash commands
# --------------------------------------------------------------------------
@bot.tree.command(name="ask", description="Ask Rukia anything.")
@app_commands.describe(question="Your question", image="Optional photo (PNG, JPG, WEBP)")
async def ask(interaction: discord.Interaction, question: str, image: Optional[discord.Attachment] = None) -> None:
    await _slash_chat(interaction, question, image=image)


@bot.tree.command(name="solve", description="Solve a problem step by step (maths, physics, logic, code, MCQs).")
@app_commands.describe(problem="The problem to solve", image="Optional photo of the question")
async def solve(interaction: discord.Interaction, problem: str, image: Optional[discord.Attachment] = None) -> None:
    await _slash_chat(interaction, problem, image=image, extra_system=config.SOLVE_PROMPT)


@bot.tree.command(name="explain", description="Explain a concept at your level.")
@app_commands.describe(topic="What should I explain?", level="How deep? (default: beginner)")
@app_commands.choices(
    level=[
        app_commands.Choice(name="beginner", value="beginner"),
        app_commands.Choice(name="intermediate", value="intermediate"),
        app_commands.Choice(name="advanced", value="advanced"),
    ]
)
async def explain(
    interaction: discord.Interaction, topic: str, level: Optional[app_commands.Choice[str]] = None
) -> None:
    key = level.value if level else "beginner"
    extra = f"{config.EXPLAIN_PROMPTS[key]}\n{config.EXPLAIN_SUFFIX}"
    await _slash_chat(interaction, f"Explain: {topic}", extra_system=extra)


@bot.tree.command(name="search", description="Search the web and answer from the results.")
@app_commands.describe(query="What to search for")
async def search_cmd(interaction: discord.Interaction, query: str) -> None:
    if not search.is_enabled():
        await interaction.response.send_message(
            "Web search is off. The bot owner needs to set `TAVILY_API_KEY` or `BRAVE_API_KEY` in `.env`.",
            ephemeral=True,
        )
        return
    await _slash_chat(interaction, query, forced_search=query)


@bot.tree.command(name="reset", description="Clear my memory of this channel's conversation.")
async def reset(interaction: discord.Interaction) -> None:
    await interaction.response.defer()  # may wait for a running answer to finish
    await bot.memory.reset(interaction.channel_id or interaction.user.id)
    await interaction.followup.send("Done! I've forgotten our conversation in this channel.")


@bot.tree.command(name="ping", description="Show the bot's latency.")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)} ms")


@bot.tree.command(name="model", description="Show which AI providers and models are active.")
async def model(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="Rukia's engines", colour=discord.Colour.blurple())
    providers = ai.describe_providers()
    for name, model_name, role in providers:
        embed.add_field(name=f"{name} ({role})", value=f"`{model_name}`", inline=False)
    if not providers:
        embed.description = "No AI provider is configured."
    embed.add_field(name="Image input", value="on" if ai.has_vision() else "off", inline=True)
    embed.add_field(name="Web search", value=search.providers_label(), inline=True)
    backend = bot.db.backend if bot.db else "RAM only"
    embed.add_field(name="Memory", value=backend, inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="How to use Rukia.")
async def help_cmd(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="Hi, I'm Rukia!",
        description=(
            "I help with assignments, maths, coding, science and writing.\n"
            "Mention me in a channel, send me a DM, or use the commands below."
        ),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="Commands",
        value=(
            "`/ask` ask anything\n"
            "`/solve` step-by-step solutions (attach a photo of the question)\n"
            "`/explain` explain a topic at your level\n"
            "`/search` search the web\n"
            "`/remember`, `/memory`, `/forget` your personal notes\n"
            "`/reset` clear this channel's conversation\n"
            "`/model`, `/ping` status"
        ),
        inline=False,
    )
    embed.add_field(
        name="Status",
        value=(
            f"Web search: {'on (' + search.providers_label() + ')' if search.is_enabled() else 'off'}\n"
            f"Image input: {'on' if ai.has_vision() else 'off'}"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="remember", description="Save a short note about yourself for Rukia.")
@app_commands.describe(note="For example: I'm a B.Tech IT student, prefer Python")
async def remember(interaction: discord.Interaction, note: app_commands.Range[str, 1, config.MAX_NOTE_LEN]) -> None:
    if bot.db is None:
        await interaction.response.send_message("Saving notes isn't available right now.", ephemeral=True)
        return
    try:
        saved = await bot.db.add_note(interaction.user.id, note.strip())
    except Exception as exc:  # noqa: BLE001
        log.warning("add_note failed: %s", exc)
        await interaction.response.send_message("I couldn't save that right now. Try again later.", ephemeral=True)
        return
    msg = (
        "Saved! I'll keep that in mind when I talk to you."
        if saved
        else f"You already have {config.MAX_NOTES} notes. Use `/forget` to delete one first."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="memory", description="Show your saved notes (only you can see this).")
async def memory_cmd(interaction: discord.Interaction) -> None:
    notes = await _load_notes(interaction.user.id)
    if not notes:
        text = "You have no saved notes. Add one with `/remember`."
    else:
        text = "**Your notes:**\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(notes, 1))
    await interaction.response.send_message(text[:1900], ephemeral=True)


@bot.tree.command(name="forget", description="Delete one saved note (by number) or all of them.")
@app_commands.describe(number="Note number from /memory. Leave empty to delete all notes.")
async def forget(interaction: discord.Interaction, number: Optional[int] = None) -> None:
    if bot.db is None:
        await interaction.response.send_message("Notes aren't available right now.", ephemeral=True)
        return
    try:
        if number is None:
            count = await bot.db.clear_notes(interaction.user.id)
            msg = f"Deleted {count} note{'s' if count != 1 else ''}."
        else:
            ok = await bot.db.delete_note(interaction.user.id, number)
            msg = f"Deleted note {number}." if ok else f"There is no note number {number}. Check `/memory`."
    except Exception as exc:  # noqa: BLE001
        log.warning("forget failed: %s", exc)
        msg = "I couldn't do that right now. Try again later."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.warning("Command error: %s", error)
    text = "Something went wrong with that command. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    if not config.DISCORD_TOKEN:
        sys.exit(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token "
            "(Discord Developer Portal -> Bot -> Reset Token). See README.md."
        )
    if not ai.is_configured():
        sys.exit(
            "No AI key found. Set at least one of GROQ_API_KEY, GEMINI_API_KEY or "
            "OPENROUTER_API_KEY in your .env file (all have free tiers). See README.md."
        )
    log.info("Active providers: %s", ", ".join(p.name for p in config.PROVIDERS))
    try:
        bot.run(config.DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        sys.exit("Discord rejected the token. Check DISCORD_TOKEN in .env (reset it in the Developer Portal).")
    except discord.PrivilegedIntentsRequired:
        sys.exit(
            "Used disallowed intents: enable 'Message Content Intent' under Bot -> Privileged "
            "Gateway Intents in the Discord Developer Portal, then restart."
        )


if __name__ == "__main__":
    main()
