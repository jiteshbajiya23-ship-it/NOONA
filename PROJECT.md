# 📘 NOONA: Project Documentation

Technical notes for the NOONA bot. For setup and usage, see `README.md`.

---

## 🎯 Purpose

NOONA is an owner-only Telegram bot that monitors websites through the UptimeRobot API. It alerts the owner on status changes, sends a daily report, and lets the owner manage monitors from chat.

**Persona:** flirty, romantic, and loyal to her owner. All user-facing text is in English.

---

## 🧱 Tech Stack

| Part | Choice |
|---|---|
| Language | Python 3.10+ |
| Telegram | `python-telegram-bot` with the `job-queue` extra |
| HTTP client | `httpx` (async) |
| Web server | `aiohttp` (health endpoint) |
| Monitoring data | UptimeRobot API v2 |
| Hosting | Render (Web Service) |

---

## 🗂 Structure of `bot.py`

| Section | What it contains |
|---|---|
| **Fonts** | `script()` and `soft()` convert A-Z / a-z into Unicode font styles |
| **Config** | Environment variables, constants, status labels |
| **Helpers** | `esc`, `api`, `err`, `fmt_dur`, `fmt_t`, `send_html` |
| **Text builders** | `text_monitors`, `text_status`, `build_report`, `text_help`, `menu_keyboard` |
| **Security guard** | `guard` blocks everyone except the owner |
| **Commands** | Registered with the `@command` decorator |
| **Inline buttons** | `on_button` handles the `m:*` callbacks |
| **Background jobs** | `check_job` (alerts) and `daily_job` (report) |
| **Web server** | `start_web` serves `/`, `/health`, `/ping` |
| **Main** | Builds the app, registers handlers and jobs, starts polling |

---

## 🎨 Fonts

Telegram bots cannot change typefaces, so NOONA uses two Unicode text styles:

| Helper | Style | Used for |
|---|---|---|
| `script(text)` | Mathematical Bold Script (𝓗𝓮𝓵𝓵𝓸) | Titles and headings |
| `soft(text)` | Mathematical Sans-Serif Bold Italic (𝙃𝙚𝙡𝙡𝙤) | Labels and captions |

Only letters are converted. Digits, emojis, punctuation, IDs, names, and URLs stay in plain text so they remain readable and copyable. Apply the helpers only to **static text you write**, never to user data.

---

## 🔐 Security

- A `TypeHandler` in group `-1` runs before every other handler.
- If the sender is not `OWNER_ID`, it replies with `DENY_TEXT` and raises `ApplicationHandlerStop`.
- Inline button presses from non-owners get the denial as an alert.
- The command menu is registered only for the owner's chat.
- Secrets come from environment variables only and are never committed.

---

## 🔔 Alert Logic

`check_job` runs every `CHECK_EVERY` seconds.

1. Fetch all monitors from UptimeRobot.
2. Compare each monitor's status with the last known status (kept in memory).
3. **Up → Down (status 8 or 9):** send a DOWN alert and record the start time.
4. **Down → Up (status 2):** send a BACK UP alert with the downtime duration.
5. On the first run after a restart, no alerts are sent. Current states are only recorded.
6. Monitors that no longer exist are removed from memory.

State lives in memory only, so a restart forgets how long a monitor has been down.

---

## 📊 Reports

- **Daily report:** runs at 00:00 in `TZ_NAME` and covers the previous full day.
- **On demand:** `/report [hours]` covers the last N hours (1 to 720, default 24).
- Each monitor shows its uptime percentage and a list of down periods with start and end times.

---

## 🌐 Web Server

| Route | Response |
|---|---|
| `/`, `/health`, `/ping` | `{"status": "ok", "bot_uptime_sec": <int>}` |

Render requires an open port, and UptimeRobot pings `/health` to keep the service awake.

---

## ⚙️ Environment Variables

| Variable | Required | Default |
|---|---|---|
| `BOT_TOKEN` | ✅ | n/a |
| `UPTIMEROBOT_API_KEY` | ✅ | n/a |
| `OWNER_ID` | ✅ | n/a |
| `TZ_NAME` | ❌ | `Asia/Kolkata` |
| `PORT` | ❌ | `10000` |
| `CHECK_EVERY` | ❌ | `60` |
| `WELCOME_IMAGE` | ❌ | built-in URL |
| `NOTIFY_STARTUP` | ❌ | `1` |

---

## 🧩 Adding a Feature

1. Write an async function that takes `(update, context)`.
2. Decorate it with `@command("name", "Short description 💋")`.
3. The command is added to the Telegram menu and `/help` automatically.
4. For long output, use `send_html()` so messages split safely at 3800 characters.
5. Keep the persona: English only, warm, flirty, and with emojis. Use `script()` for titles and `soft()` for labels.

---

## ⚠️ Notes and Limitations

- Alert state is in memory only (resets on restart or redeploy).
- The bot uses long polling, so run only **one instance** at a time.
- `drop_pending_updates=True` discards messages sent while the bot was offline.
- The startup message fails quietly if the owner has never sent `/start`.
- Unicode font styles can look different across devices, and screen readers may read them poorly.

---

## 🗺 Ideas for Later

- Persist alert state in a small database or file
- Alert on SSL expiry
- Per-monitor mute and snooze buttons
- Weekly summary report 💌
