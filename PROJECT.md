# PROJECT.md — Uptime Guardian (Telegram Multi-Function Bot)

> **Note for Claude:** Read this file fully before touching the code. The owner (Hinglish speaker, mobile user) will paste/upload the latest `bot.py` along with this file. Continue from here, keep the rules below, and update this file when features change.

---

## 1. Goal
A private, **owner-only** Telegram bot hosted on **Render** that monitors everything on **UptimeRobot**, alerts on down/up, and sends a daily report. It is the base for a bigger **multi-function bot** — more features will be added over time.

## 2. Tech Stack
- Python 3.11+
- `python-telegram-bot[job-queue]==21.6` (async, polling)
- `httpx` (UptimeRobot API calls)
- `aiohttp` (health web server for Render + UptimeRobot)
- Hosting: Render **Web Service** (free tier ok)
- Files: `bot.py`, `requirements.txt`, `PROJECT.md`

## 3. Environment Variables (Render → Environment)
| Name | Required | Meaning |
|---|---|---|
| `BOT_TOKEN` | ✅ | Token from @BotFather |
| `UPTIMEROBOT_API_KEY` | ✅ | UptimeRobot **Main API Key** (read-write; needed for add/remove/pause) |
| `OWNER_ID` | ✅ | Owner's numeric Telegram ID |
| `TZ_NAME` | ❌ | Timezone, default `Asia/Kolkata` |
| `CHECK_EVERY` | ❌ | Monitor poll interval in sec, default `60` |
| `WELCOME_IMAGE` | ❌ | Image URL for /start, default `https://imgyx.pages.dev/ZkcBn` |
| `NOTIFY_STARTUP` | ❌ | `1` = message owner on each start, `0` = off |
| `PYTHON_VERSION` | ✅ (Render) | Set `3.12.8` (Render defaults to 3.14 which breaks old PTB) |
| `PORT` | auto | Set by Render |

## 4. Deploy on Render
1. Push files to GitHub.
2. Render → New → **Web Service** → connect repo.
3. Build: `pip install -r requirements.txt`
4. Start: `python bot.py`
5. Add the env vars above.
6. In **UptimeRobot** add an HTTP monitor → `https://<your-service>.onrender.com/health` (5 min interval). This keeps Render awake and monitors the bot itself.
7. Owner must send `/start` to the bot once (so the bot can DM the owner).

## 5. How It Works
- **Owner guard:** `TypeHandler(Update, guard)` in group `-1` runs before every handler. If `user.id != OWNER_ID`, bot replies `I Gave My Body To My Owner` and stops processing (`ApplicationHandlerStop`). Works for commands, messages (private chat) and button clicks.
- **Command registry:** `@command("name", "description")` decorator registers handler + adds it to /help and the Telegram command menu (menu is set only for owner via `BotCommandScopeChat`).
- **Alerts:** `check_job` runs every `CHECK_EVERY` sec, calls UptimeRobot `getMonitors`, compares with `last_status` (in-memory). Status `2` = up, `8`/`9` = down, `0` = paused. First run after boot is silent (just loads state).
- **Daily report:** `daily_job` at 00:00 (`TZ_NAME`) builds a report for the previous day using UptimeRobot logs (`logs=1`, `logs_start_date`, `logs_end_date`, `custom_uptime_ranges`). No database needed; history survives restarts.
- **Health server:** aiohttp serves `/`, `/health`, `/ping` returning JSON. GET and HEAD work (UptimeRobot-friendly).
- **/start:** owner gets photo + welcome caption + inline menu buttons. If the image fails, falls back to text.
- All messages use **HTML parse mode**; always wrap dynamic text with `esc()`.

## 6. Commands (owner only)
| Command | Description |
|---|---|
| `/start` | Welcome photo + menu buttons |
| `/help` | List of commands (auto-generated) |
| `/status` | Summary: up / down / paused counts |
| `/monitors` | All monitors with ID, status, URL |
| `/add <name> <url>` | Create HTTP monitor (5 min interval) |
| `/remove <id>` | Delete monitor |
| `/pause <id>` / `/resume <id>` | Pause / resume monitor |
| `/report [hours]` | Report for last N hours (default 24, max 720) |
| `/ping` | Bot uptime + UptimeRobot API latency |

Automatic: 🔴 down alert, 🟢 back-up alert (with downtime duration), 📊 daily report at 12:00 AM.

## 7. How To Add A New Feature
```python
@command("mycmd", "What it does")
async def mycmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello owner")
```
- Put new commands in the **COMMANDS section** of `bot.py`.
- Need a new button? Add to `menu_keyboard()` with `callback_data="m:<key>"` and handle in `on_button()`.
- Need a new schedule? `app.job_queue.run_repeating / run_daily` in `main()`.
- New pip package? Add to `requirements.txt`.
- When the file gets too big, split into modules (`features/`), keeping the registry pattern.

## 8. Rules / Decisions (do not break)
1. **Owner-only, always.** Every new feature must sit behind the guard. Never add a handler that bypasses it.
2. Non-owner reply text is exactly: `I Gave My Body To My Owner`.
3. Keep the **health endpoint** alive — Render needs a bound port and UptimeRobot triggers it.
4. Never hardcode secrets; use env vars.
5. Free Render filesystem is ephemeral — don't store important data in local files. If persistence is needed, use a DB (Render Postgres / MongoDB Atlas / Supabase) and note it here.
6. UptimeRobot free plan: 50 monitors, 5-min interval, API limit 10 req/min — keep `CHECK_EVERY >= 60`.
7. Reply style for the owner: Hinglish, short, mobile-friendly.

## 9. Known Limitations
- `last_status` / `down_since` live in memory: if the bot restarts while a monitor is down, the "DOWN" alert is not repeated (the "BACK UP" alert still comes, duration counted from restart).
- Down detection is as fast as `CHECK_EVERY` (UptimeRobot's own check interval is 5 min on free plan, so real delay can be up to ~5–6 min).
- Render free tier sleeps if nothing pings it → the UptimeRobot monitor on `/health` is required.

## 10. Roadmap / Ideas (not built yet)
- Alert on SSL expiry / domain expiry
- Weekly + monthly report
- Inline buttons for pause/resume/delete per monitor
- Multiple admins (list of IDs)
- Response-time graphs (chart image)
- Other utilities: reminders, notes, broadcast, downloader, etc. (owner will decide)

## 11. Changelog
- v1: UptimeRobot alerts, daily report, add/remove monitors, owner-only.
- v2.1: Python 3.14 event-loop fix (asyncio.set_event_loop) + `.python-version` pinned to 3.12.8.
- v2: /start photo + menu, non-owner deny message, pause/resume, /status, /ping, /report [hours], command registry, health endpoint for UptimeRobot, this PROJECT.md.
