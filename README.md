# MyAgent

A personal Telegram AI agent with a terminal, long-term memory, voice replies and
its own server body. One agent, one context, many toggles.

Built on Python 3.14 + Aiogram 3 + OpenAI-compatible API (AITunnel), running as a
systemd user service.

## What it does

- **Telegram bot** — the owner's replies are delivered automatically as the final
  text; `telegram_send` is for addressing someone else by name. Regular users get
  a simple "one question — one answer" contract.
- **Agent loop** — reasoning model with tool calling, retry/resilience on 5xx and
  timeouts, hybrid history (SQLite + in-memory cache).
- **Server access** — `run_command` (bash), file tools (`read_file`, `edit_file`,
  `write_file`, `search_code`) gated by a toggle.
- **Long-term memory** — `memory_save` / `memory_load` / `memory_forget` on a
  separate SQLite DB. Always available, even with the terminal off.
- **Voice** — TTS replies via `send_voice` (wav → ogg/opus with a bundled static
  ffmpeg), toggle with the «Голос» button.
- **Reminders & schedule** — background loop fires reminders every 30 s
  (`reminder_add`, `reminder_list`, `reminder_delete`), Moscow time.
- **Web access** — `web_search` and `web_fetch` behind the «Поиск» toggle.

## Toggles

| Button      | Effect when off                                  |
|-------------|--------------------------------------------------|
| Мышление    | Reasoning disabled (faster, cheaper replies)     |
| Терминал    | No bash / file tools                             |
| Пользователи| User messages never reach the agent              |
| Поиск       | `web_search` / `web_fetch` unavailable           |
| Голос       | Text replies instead of voice notes              |
| `/price on` | Cheapest provider routing (`off` = lowest latency)|

## Registration flow

1. A stranger sends `/start` → a `pending` request appears.
2. The agent reviews it and asks the owner: yes or no.
3. On approval the user's first message is stored as their display name —
   that name is also how you address them in `telegram_send`.

## Sending by name

`telegram_send` resolves recipients by name, so you never hardcode IDs:

```json
{"text": "hey", "name": "Lera"}   // → resolved from the users table
{"text": "hey"}                   // → whoever wrote last
```

Unknown or ambiguous names return a friendly error instead of crashing.

## Setup

```bash
# 1. Environment
cat > .env <<'EOF'
BOT_TOKEN=...
AITUNNEL_API_KEY=...
ALLOWED_USER_ID=...
MODEL_NAME=qwen3.8-flash
EOF

# 2. Dependencies
python3 -m venv venv && venv/bin/pip install -r requirements.txt

# 3. Run
venv/bin/python agent.py
```

As a systemd user service:

```bash
systemctl --user start myagent
journalctl --user -u myagent -f
```

## Layout

```
agent.py           # agent loop, tool schemas, API client
bot.py             # Aiogram bot, toggles, delivery, name resolution
tools.py           # tool implementations (files, memory, web, voice, reminders)
phrases.py         # UI strings
system_prompt.txt  # agent persona (git-ignored, personal)
*.db               # history, states, memory, reminders (git-ignored)
bin/ffmpeg         # local static ffmpeg copy (git-ignored)
```

## Safety

- `.env`, databases, backups and `system_prompt.txt` are git-ignored.
- `telegram_send` is owner-only; destructive requests are refused for everyone.
- The terminal is hard-wired to the owner account.

## License

Private project. All rights reserved.
