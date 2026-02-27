# 🚋 TramTram

Real-time Turin public transport (GTT) tracker on Telegram.

**Just want to use it?** Open [@tramtramgtt\_bot](https://t.me/tramtramgtt_bot) on Telegram and press Start — no setup needed.

If the hosted bot is no longer available, you can self-host your own instance using this repository (see [Self-hosting](#self-hosting) below).

---

## What it does

TramTram monitors GTT bus and tram arrivals in real time via the [Muoversi a Torino](https://www.muoversiatorino.it/) OpenTripPlanner API and keeps Telegram messages updated in place every 15 seconds.

Each user gets their own independent dashboard — trips and data are fully isolated.

### Features

| Feature | Description |
|---|---|
| **Live dashboard** | One message per trip, edited in place every 15 s with next arrivals. Green dot (🟢) = GPS realtime. |
| **Quick stop query** | Send any stop number to see all lines at that stop, live for 15 minutes, with a STOP button to dismiss. |
| **Add/remove wizard** | `/add` and `/remove` guide you step by step — no config files to edit. |
| **Multi-user** | Every Telegram user has their own trips; data is stored per chat ID. |
| **Persistent state** | Trips and message IDs survive bot restarts. |
| **Night pause** | No API calls between 02:00 and 07:00 (configurable). |

### Commands

| Command | What it does |
|---|---|
| `/start` | Clean up old messages and (re)create the live dashboard |
| `/add` | Wizard to add a new trip or combo |
| `/remove` | Wizard to remove a trip or combo |
| `/refresh` | Force an immediate dashboard update |
| `/cancel` | Abort the current wizard |
| `<number>` | Send a stop ID to get live arrivals for 15 minutes |

### How trips work

You organize your monitored routes into **trips**, **combos**, and **legs**:

```
Trip  (e.g. "Home → Office")
 └── Combo  (e.g. "Direct 42", "Combo 16 + 4")
      └── Leg
           ├── line               (e.g. "42")
           ├── stop_id_boarding   (where you get on)
           └── stop_id_alighting  (where you get off)
```

- A **trip** is a named origin-destination pair (e.g. "Home → Office").
- A **combo** is one way to make that trip — it can have one or more legs (direct or with transfers).
- A **leg** is a single bus/tram ride: which line, where you board, and where you alight.

All of this is configured interactively through the `/add` wizard. To find GTT stop IDs, send any number to the bot and it will show you the stop name, or look them up on [Muoversi a Torino](https://www.muoversiatorino.it/).

### Example output

**Dashboard:**

```
🚋  Home → Office
⏱  08:32:15

━━━  Direct 42  ━━━

  🚌  42
        OSPEDALE MAURIZIANO  ➜  PORTA NUOVA
        ⏳  🟢3'   🟢15'   30'
```

**Quick stop query:**

```
🚏  PORTA NUOVA  (40)
⏱  08:32:15
⏳  expires in 14 min

  🚌  42  ➜  SASSI
        ⏳  🟢5'   🟢18'   32'

  🚌  66  ➜  LINGOTTO
        ⏳  🟢2'   12'

  🚌  4  ➜  FALCHERA
        ⏳  🟢8'

[🛑 STOP]
```

---

## Self-hosting

If the public bot goes offline, you can run your own instance. All you need is a server (or even your own computer), Python, and a Telegram bot token.

### Requirements

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Quick start

```bash
git clone https://github.com/lucaosti/tramtram.git
cd tramtram

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and set your BOT_TOKEN

python main.py
```

### Project structure

```
tramtram/
├── main.py              # Entire bot (single-file)
├── .env                 # BOT_TOKEN (git-ignored, you create this)
├── .env.example         # Template for .env
├── config.json          # Optional global settings (git-ignored)
├── data/                # Per-user data (git-ignored, auto-created)
│   └── <chat_id>.json   # One file per Telegram user
├── requirements.txt     # Python dependencies
├── README.md
└── .gitignore
```

### Configuration

#### Bot token (`.env`, required)

The only required configuration. Create a `.env` file (or export the variable) with your bot token:

```
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
```

#### Global settings (`config.json`, optional)

If this file is absent, sensible defaults are used. Create it only to customize behavior:

```json
{
  "otp_base_url": "https://plan.muoversiatorino.it/otp/routers/mato/index",
  "polling_interval_seconds": 15,
  "night_pause": { "start_hour": 2, "end_hour": 7 }
}
```

| Field | Description | Default |
|---|---|---|
| `otp_base_url` | Base URL of the OTP API | `https://plan.muoversiatorino.it/otp/routers/mato/index` |
| `polling_interval_seconds` | Seconds between dashboard updates | `15` |
| `night_pause.start_hour` | Hour (0–23) when the bot pauses API calls | `2` |
| `night_pause.end_hour` | Hour (0–23) when the bot resumes | `7` |

#### Per-user data (`data/`)

Each user's trips and message state are automatically saved in `data/<chat_id>.json`. This directory is created on first use — no manual setup needed.

Example file (`data/123456789.json`):

```json
{
  "trips": [
    {
      "name": "Home → Office",
      "combos": [
        {
          "name": "Direct 42",
          "legs": [
            { "line": "42", "stop_id_boarding": "1132", "stop_id_alighting": "40" }
          ]
        }
      ]
    }
  ],
  "state": {
    "dashboard_msgs": [101, 102],
    "stop_msgs": {},
    "all_msg_ids": [101, 102]
  }
}
```

### Architecture

The bot is a single Python file (`main.py`) built on [python-telegram-bot](https://python-telegram-bot.org/) and [httpx](https://www.python-httpx.org/).

**Data flow:**

1. On startup, the bot loads all user files from `data/` and starts a background update loop.
2. The update loop runs every 15 seconds (skipping night hours). It collects all stop IDs needed across every active user, fetches stoptimes and stop names from the OTP API in a single parallel batch, and edits each user's Telegram messages with fresh data.
3. When a user sends `/start`, old messages are deleted and new dashboard messages are created (one per trip).
4. When a user sends a stop number, a live message is created that auto-updates for 15 minutes, then self-destructs.

**Key design decisions:**
- Messages are edited in place (no spam) and deleted on cleanup.
- Stop IDs are deduplicated across users, so the same stop is only fetched once per cycle regardless of how many users monitor it.
- Wizards use a single-message editing pattern: one bot message is reused for every step, and user messages are deleted immediately.
- The Europe/Rome timezone is computed manually (EU DST rules) to avoid a `pytz`/`zoneinfo` dependency.

### Running on a server

To keep the bot running permanently on a Linux server, use systemd:

Create `/etc/systemd/system/tramtram.service`:

```ini
[Unit]
Description=TramTram Telegram Bot
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/tramtram
EnvironmentFile=/path/to/tramtram/.env
ExecStart=/path/to/tramtram/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tramtram
sudo systemctl start tramtram

# Check status
sudo systemctl status tramtram

# View logs
journalctl -u tramtram -f
```

### Migration from single-user version

If you're upgrading from an older version that had `bot_token` and `chat_id` inside `config.json`, the bot handles migration automatically on first startup:

1. Trips are moved to `data/<chat_id>.json`.
2. `config.json` is rewritten with only global settings.
3. `state.json` is deleted.

You just need to create the `.env` file with your `BOT_TOKEN`.

---

## Dependencies

| Package | Purpose |
|---|---|
| [python-telegram-bot](https://python-telegram-bot.org/) | Telegram Bot API framework |
| [httpx](https://www.python-httpx.org/) | Async HTTP client for OTP API calls |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | Load `.env` file into environment variables |

## License

MIT
