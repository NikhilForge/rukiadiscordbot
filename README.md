# Rukia: a Discord AI assistant bot

## 1. What is Rukia?

Rukia is a friendly AI assistant for a private Discord server. She helps with school and college assignments, maths (step by step), coding in any language, science, writing and general questions. She has **no per-user cooldown**.

Features:

- Chat via slash commands, @mentions in a channel, or direct messages
- **Photo input**: attach a picture of a maths problem, diagram or code screenshot
- **Web search** for current questions, with a short *Sources:* list
- **Persistent memory** (Supabase Postgres, or a local SQLite file as fallback): conversation history and your `/remember` notes survive restarts
- **Automatic fallback** between AI providers (Groq, then Gemini, then OpenRouter)
- Long answers are split safely, and code blocks are never broken across messages

## 2. Requirements

- Python **3.10 or newer** (check with `python --version`)
- A Discord account and a server where you can add bots
- At least one free AI API key (see section 4)

## 3. Discord setup (step by step)

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it **Rukia**.
2. (Optional) Under **General Information**, upload your own avatar. Use your own artwork; this project ships no character images or logos.
3. Open the **Bot** tab and click **Reset Token**, then **copy the token**. This is your `DISCORD_TOKEN`. Keep it secret.
4. On the same **Bot** tab, scroll to **Privileged Gateway Intents** and turn on **Message Content Intent**. Click **Save Changes**. Without this, mentions and DMs will not work and the bot may stop with a "Used disallowed intents" error.
5. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: **View Channels**, **Send Messages**, **Read Message History**, **Embed Links**
6. Copy the generated URL at the bottom, open it in your browser, choose your server and click **Authorize**.

## 4. Getting free API keys

| Key | Where | Needed for |
|---|---|---|
| `GROQ_API_KEY` | <https://console.groq.com> | Fast text chat (primary provider) |
| `GEMINI_API_KEY` | <https://aistudio.google.com> | Image input; text fallback |
| `TAVILY_API_KEY` | <https://tavily.com> | Web search |
| `OPENROUTER_API_KEY` | <https://openrouter.ai> | Optional extra fallback |
| `BRAVE_API_KEY` | <https://brave.com/search/api/> | Optional second search provider |

**Which feature needs which key**

- **Text chat** needs **any one** AI key (Groq, Gemini or OpenRouter).
- **Images** need **Gemini** (or a vision model set in `GROQ_VISION_MODEL`).
- **Web search** needs **Tavily or Brave**. With neither key, search is simply off.

The bot starts with just one key. A provider is active only if its key is set.

## 5. Supabase setup (persistent memory)

Supabase gives you a free hosted Postgres database so Rukia remembers conversations and notes across restarts.

1. Create a free account and a new project at <https://supabase.com>.
2. Choose a **database password** when asked, and **save it** somewhere safe.
3. Open **Project Settings → Database**.
4. Under **Connection string**, copy the **Session pooler** string. (Do not use the "Direct connection" string: it is IPv6-only on many networks and fails on IPv4-only hosts.)
5. In that string, replace the `[YOUR-PASSWORD]` placeholder with your real password. If the password contains special characters such as `@`, `:`, `/` or `#`, **URL-encode** them (for example `@` becomes `%40`).
6. Paste the result into `.env` as `DATABASE_URL=...`.

On first start the bot **creates its tables automatically** (`messages` and `user_notes`). You can see them afterwards under **Table Editor** in the Supabase dashboard. Row Level Security is enabled on both tables with no policies, so they cannot be read through Supabase's public REST API or the anon key. The bot's own direct connection is not affected.

### Notes on persistent memory

- With Supabase, history and `/remember` notes **survive restarts** and work on **any host**, including Render's free tier.
- Free Supabase projects may be **paused after a period of inactivity** and must be restored from the dashboard. Check Supabase's current free-tier rules.
- If `DATABASE_URL` is empty, the bot uses a local `rukia.db` file (already excluded by `.gitignore`). On hosts with an **ephemeral filesystem** this file is erased on every restart.
- If Supabase cannot be reached at startup, the bot logs the error and falls back to SQLite. If it fails while running, the bot keeps answering with RAM-only history.
- **Never share the connection string**: it contains your database password.
- Notes are saved only when you run `/remember`. Rukia never extracts personal facts on her own, and never shows one user's notes to another. Please do not store passwords or other secrets in notes.

## 6. Setup

1. Copy `.env.example` to `.env` and fill it in (at least `DISCORD_TOKEN` and one AI key).
2. Start the bot:
   - **Windows:** double-click `run.bat`
   - **Linux/macOS:** `./run.sh` (run `chmod +x run.sh` first if needed)
   - **Manually:**
     ```bash
     pip install -r requirements.txt
     python bot.py
     ```

## 7. Command reference

| Command | What it does |
|---|---|
| `/ask question:<text> [image]` | Ask Rukia anything |
| `/solve problem:<text> [image]` | Step-by-step solution with a **bold final answer** and a quick check |
| `/explain topic:<text> level:<beginner\|intermediate\|advanced>` | Explain a concept (default: beginner), ending with a key takeaway |
| `/search query:<text>` | Force a web search and answer from the results |
| `/remember note:<text>` | Save a short note about you (max 10 notes, 200 characters each) |
| `/memory` | Show your notes (only you can see them) |
| `/forget [number]` | Delete one note by number, or all notes |
| `/reset` | Clear this channel's conversation memory |
| `/model` | Show active providers and models (never shows keys) |
| `/ping` | Show latency |
| `/help` | Short usage guide |

You can also **@mention** Rukia in a server channel, or **DM** her (no mention needed). Attach a PNG, JPG or WEBP image (up to 4 images, 8 MB each) to `/ask`, `/solve`, a mention or a DM.

## 8. How the fallback chain works

| Priority | Provider | Default model | Key |
|---|---|---|---|
| 1 | Groq | `openai/gpt-oss-120b` | `GROQ_API_KEY` |
| 2 | Google Gemini | `gemini-3.5-flash` | `GEMINI_API_KEY` |
| 3 | OpenRouter (optional) | `nvidia/nemotron-3.5-lightning:free` | `OPENROUTER_API_KEY` |

- Rukia tries providers in order. Each provider gets **one retry** (about 1 second later) before she moves on.
- She moves on after: rate limits (HTTP 429), timeouts, connection errors and 5xx server errors. Each call times out after 60 seconds.
- She does **not** fall back on a 401/403 (bad key). She logs a clear warning in the console instead, so you notice and fix the key.
- If a provider rejects tool calling (used for web search), she silently retries it without tools.
- Requests with images use Gemini first, then `GROQ_VISION_MODEL` if you set one.
- If everything fails, she replies with a short friendly message, never a stack trace. The provider that answered is logged to the console only.

**Changing models:** set `GROQ_MODEL`, `GEMINI_MODEL`, `OPENROUTER_MODEL` or `GROQ_VISION_MODEL` in `.env`, then restart. Use `/model` to check what is active.

## 9. Hosting

Rukia needs to run all the time. Good options:

- Your own PC (simple, but it must stay on)
- An **Oracle Cloud Always Free** VM
- A cheap VPS

Free web hosts that **go to sleep** when idle are **not suitable**, because the bot would go offline.

### systemd service (Linux)

Create `/etc/systemd/system/rukia.service` (adjust the user and paths):

```ini
[Unit]
Description=Rukia Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/rukia-bot
ExecStart=/home/ubuntu/rukia-bot/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Run `./run.sh` once first so the `venv` folder exists (stop it with Ctrl+C), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rukia
sudo journalctl -u rukia -f   # view logs
```

### Docker

```bash
docker build -t rukia-bot .
docker run -d --name rukia --restart unless-stopped --env-file .env rukia-bot
```

The `.env` file is passed at run time and is never copied into the image. Use Supabase (`DATABASE_URL`) so memory survives container rebuilds.

## 10. Security notes

- **Never share or commit** your Discord token, API keys or `DATABASE_URL`. `.env` is excluded by `.gitignore`.
- If your token leaks, open the Developer Portal → **Bot** → **Reset Token** right away and update `.env`. Rotate any leaked API keys in each provider's console too.
- Web results are treated as untrusted data; Rukia is instructed never to follow instructions found inside them.
- Rukia cannot ping `@everyone`, `@here` or roles, even if a model tries to.

## 11. Troubleshooting

- **Bot is offline:** check the console for errors. A wrong `DISCORD_TOKEN` shows "Discord rejected the token". Make sure the bot process is actually running.
- **Slash commands not showing:** they sync at startup and can take a few minutes to appear. Confirm the invite link included the `applications.commands` scope, restart Discord (Ctrl+R) and try again.
- **"Used disallowed intents" error:** turn on **Message Content Intent** in the Developer Portal (Bot tab), save, and restart the bot.
- **Bot ignores @mentions or DMs:** same cause as above; also make sure she can **View Channels** and **Send Messages** there.
- **429 errors / "rate limit":** you hit a free-tier limit. Rukia retries once and then uses the next provider. Add a second provider key, or wait a bit.
- **Model not found / 404:** the model name changed. Set a current one in `.env` (`GROQ_MODEL`, `GEMINI_MODEL`, `OPENROUTER_MODEL`) using the names shown in each provider's console.
- **"Image input needs a GEMINI_API_KEY":** add `GEMINI_API_KEY` (or `GROQ_VISION_MODEL`) to `.env`.
- **Supabase connection fails:** use the **Session pooler** string, check the password (URL-encoded), and check the project is not paused. The bot falls back to SQLite meanwhile.

## 12. Free-tier limits change

Free-tier limits and model names change often. Check each provider's console (Groq, Google AI Studio, OpenRouter, Tavily, Brave, Supabase) for current limits and model names, and update `.env` when needed.
