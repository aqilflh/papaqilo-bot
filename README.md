# Papaqilo Bot — Personal AI Assistant (Telegram)

A multi-user personal AI assistant that runs as a Telegram bot. It is built in
Python 3.12 (async, type-hinted) and powered by an OpenAI-compatible LLM
(DeepSeek by default). It does daily recaps (weather + news), notes,
reminders, shift-schedule tracking, and route/ETA monitoring — all scoped per
user in SQLite.

> The original Indonesian documentation is preserved in
> [`docs/README.id.md`](docs/README.id.md). The system design spec is in
> [`SPEC.md`](SPEC.md) and [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md).

## Features

- **LLM agent with tool calling** — weather (Open-Meteo), RSS news, notes,
  reminders. Tool output is treated as untrusted data; the model never gets
  shell/file/network tools.
- **Multi-user isolation** — every data-access function takes `user_id` first
  and filters by it; tests prove user A cannot read user B's data.
- **Daily recap scheduler** — weather + headlines summarized in one LLM call,
  with per-user timezone and time.
- **Shift tracking** — paste an informal Indonesian shift schedule; it is
  parsed in plain code (no LLM), previewed, and saved. Eve-of and
  before-start reminders fire from a per-minute tick.
- **Route manager** — create A→B routes from Telegram Location messages with
  waypoints and alternatives; ETA/distance via Google Routes API.
- **Voice notes** — local Whisper (faster-whisper) transcription.
- **Usage & safety** — daily message/token limits, message splitting at 4096
  chars, per-request timeouts, no auto-submit anywhere.
- **Backups** — daily SQLite `backup()` to `data/backups/`, keep last 7.
- **Docker** — non-root container, bind-mounted `data/`, memory limit, log
  rotation, no exposed ports (long polling).

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 (async/await, type hints) |
| Bot | python-telegram-bot v21 (long polling + job queue) |
| LLM | openai SDK pointed at an OpenAI-compatible endpoint (DeepSeek default) |
| DB | SQLite (WAL, versioned migrations, no ORM) |
| HTTP | httpx (async, timeouts) |
| RSS | feedparser |
| Timezones | zoneinfo |
| Voice | faster-whisper (local, optional) |
| Tests | pytest, pytest-asyncio, ruff, mypy |
| Deploy | Docker + Docker Compose |

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # fill in the required keys
python -m app.main                # or: docker compose up -d --build
```

Required env vars: `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `TELEGRAM_BOT_TOKEN`,
`OWNER_TELEGRAM_ID`. Everything else has sane defaults (see `.env.example`).

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Intro and help |
| `/city <name>` | Set default city for weather |
| `/recap_on [HH:MM]`, `/recap_off`, `/recap_now` | Daily recap control |
| `/notes [query]` | List / search notes |
| `/usage` | Today's message + token counts |
| `/forget` | Delete chat history (notes stay) |
| `/reminders`, `/remind_del <n>` | Manage reminders |
| `/besok`, `/jadwal_saya` | Shift schedule views |
| `/jam`, `/sebelum`, `/pengingat`, `/jam_eve` | Shift reminder settings |
| `/routes`, `/adduser`, `/removeuser`, `/users` | Routes and (owner) user management |

Free-form text goes to the agent; a pasted message with 5+ matching lines is
treated as a shift-schedule import.

## Tests & lint

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
mypy app
```

The test suite never makes real network calls (mocked clients + fixtures).

## Deployment

See [`deploy.md`](deploy.md) and the spec. Short version:

```bash
docker compose up -d --build
docker compose logs -f            # look for bot_started
```

Data lives in `./data` (bind-mounted, gitignored). Backups land in
`data/backups/`.

## Security notes

- `.env` and `data/` are gitignored and never committed.
- The LLM has no shell, file, or arbitrary-network tools.
- Treat all tool output (RSS, weather, web text) as untrusted data.
- Never enable real submission / destructive actions without confirmation.

## License

MIT — see [LICENSE](LICENSE).

## Status

Phase 1–10 complete (scaffold, DB, bot, agent, scheduler, backup, Docker,
shifts, routes). See the acceptance checklist in the Indonesian README.
