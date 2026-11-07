# 📘 NOONA: Project Documentation

Technical notes for the NOONA bot. For setup and usage, see `README.md`.

---

## 🎯 Purpose

NOONA is an owner-only Telegram bot that monitors websites through the UptimeRobot API. It alerts the owner on status changes, sends a daily report, and lets the owner manage monitors from chat. Through a small WordPress plugin she also creates and edits episode posts on the owner's website.

**Persona:** flirty, romantic, and loyal to her owner. All user-facing text is in English.

---

## 🧱 Tech Stack

| Part | Choice |
|---|---|
| Language | Python 3.10+ |
| Telegram | `python-telegram-bot` 21.x to 22.x with the `job-queue` extra (`>=21.0,<23`) |
| HTTP client | `httpx` (async) |
| Web server | `aiohttp` (health endpoint) |
| Monitoring data | UptimeRobot API v2 |
| Hosting | Render (Web Service) |
| Website | WordPress REST plugin `wordpress/xiaobhai-episode-api.php` (v1.2), called from `wp.py` |
| Posters | TMDB API (optional) |

---

## 🗂 Structure of `bot.py`

| Section | What it contains |
|---|---|
| **Fonts** | Imported from `ui.py` (`script()` and `soft()`) |
| **Config** | Environment variables, constants, status labels |
| **Helpers** | `esc`, `api`, `get_monitors` (paginated), `err`, `fmt_dur`, `fmt_t`, `retry_tg`, `send_html` |
| **Text builders** | `text_monitors`, `text_status`, `build_report`, `text_help`, `menu_keyboard` |
| **Security guard** | `guard` blocks everyone except the owner |
| **Commands** | Registered with the `@command` decorator |
| **Inline buttons** | `on_button` handles the `m:*` callbacks: menu, `m:wpnew`, `m:wpedit`, and the alert buttons `m:chk:<id>`, `m:pau:<id>`, `m:ack` |
| **Background jobs** | `startup_job` (command menu + online message), `check_job` (alerts) and `daily_job` (report) |
| **Error handler** | `on_error` logs network blips as warnings and real bugs as errors |
| **Web server** | `start_web` serves `/`, `/health`, `/ping` |
| **Main** | Builds the app, registers handlers and jobs, starts polling |

---

## 🎨 Fonts

Telegram bots cannot change typefaces, so NOONA uses two Unicode text styles:

Both helpers live in `ui.py`, together with `head()` (emoji + script title), the `DIV` divider and a few random romantic phrases (`done()`, `oops()`, `wait()`, `cancelled()`).

| Helper | Style | Used for |
|---|---|---|
| `script(text)` | Mathematical Bold Script (𝓗𝓮𝓵𝓵𝓸) | Titles and headings |
| `soft(text)` | Mathematical Sans-Serif Bold Italic (𝙃𝙚𝙡𝙡𝙤) | Labels and captions |

Only letters are converted. Digits, emojis, punctuation, IDs, names, and URLs stay in plain text so they remain readable and copyable. Apply the helpers only to **static text you write**, never to user data.

---

## 🌐 Website Module (`wp.py` + plugin)

| Piece | What it does |
|---|---|
| `WordPress` client | Calls `/wp-json/xiaobhai/v1/...` with the `X-XB-Token` header (falls back to `?rest_route=` if `/wp-json/` is blocked) |
| `/wp` flow | name → series → category → episode → embeds → TMDB poster → create. State is a `Flow` in memory (30 min idle timeout) |
| `/edit` flow | find post (link, ID, or `name ep N`) → card with buttons → one change per tap. State is an `Edit` in memory |
| ⏭ Next episode | After a post is created, one tap starts the next episode with the same series, category and poster |

**Plugin routes (v1.2):** `GET /ping`, `/series`, `/categories`, `/posts`, `/post/<id>`; `POST /calibrate`, `/episode`, `/post/<id>`.

**Callback prefixes:** `wp:*` (create flow), `ed:*` (edit flow), `m:*` (main menu and alerts).

**Editing rules**
- `POST /post/<id>` changes only the fields that are sent: `title`, `status`, `episode`, `series_id`, `category_ids`, `embeds` (the full new list, `[]` removes all), `featured_image_url`.
- Changing the title does **not** change the post URL (WordPress keeps the slug), so existing links keep working.
- Changing the episode or series is refused if another post already has that series + episode.
- Embeds the bot wrote into the post text (`<h3>Host</h3><iframe>`) are replaced; any other text in the post is left alone.
- The plugin acts as the site administrator for the request so WordPress does not strip `<iframe>` tags.

---

## 🔐 Security

- A `TypeHandler` in group `-1` runs before every other handler.
- If the sender is not `OWNER_ID`, it replies with `DENY_TEXT` and raises `ApplicationHandlerStop`.
- Inline button presses from non-owners get the denial as an alert.
- The command menu is registered only for the owner's chat.
- Secrets come from environment variables only and are never committed.
- The WordPress plugin ships with the placeholder secret `CHANGE_ME` (too short to work). Put the real secret in `wp-config.php`, never in the repo. After 10 wrong tokens an IP is locked out for 10 minutes.
- The website client only accepts poster URLs from `image.tmdb.org`.
- The `httpx` and `httpcore` loggers are set to `WARNING`. At `INFO` they print full request URLs, which include the bot token.
- If a token ever shows up in logs or a chat, revoke it in @BotFather (`/revoke`) and update `BOT_TOKEN` on Render.

---

## 🔔 Alert Logic

`check_job` runs every `CHECK_EVERY` seconds.

1. Fetch all monitors from UptimeRobot.
2. Compare each monitor's status with the last known status (kept in memory).
3. **Up → Down (status 8 or 9):** send a DOWN alert and record the start time.
4. **Down → Up (status 2):** send a BACK UP alert with the downtime duration.
5. On the first run after a restart, no alerts are sent. Current states are only recorded.
6. Monitors that no longer exist are removed from memory.
7. If one alert fails to send, it is logged and the remaining monitors are still processed.

State lives in memory only, so a restart forgets how long a monitor has been down.

---

## 📊 Reports

- **Daily report:** runs at 00:00 in `TZ_NAME` and covers the previous full day.
- **On demand:** `/report [hours]` covers the last N hours (1 to 720, default 24).
- Each monitor shows its uptime percentage and a list of down periods with start and end times.
- Reports longer than 24 hours include the date on every time, so "10:00 PM" is never ambiguous.

---

## 🌐 Web Server

| Route | Response |
|---|---|
| `/`, `/health`, `/ping` | `{"status": "ok", "bot_uptime_sec": <int>}` |

Render requires an open port, and UptimeRobot pings `/health` to keep the service awake.

---

## 🛡 Reliability

| Problem | How it is handled |
|---|---|
| Telegram calls time out on a Render cold start (default timeout is 5s) | Connect, read, write, and pool timeouts are raised to 20-30s in `main()` |
| Startup calls (`set_my_commands`, online message) still fail | `retry_tg` retries up to 5 times with growing delays and never raises |
| APScheduler skips a job that starts more than 1s late | Each job has a `misfire_grace_time` (`startup_job` 120s, `check_job` 30s, `daily_job` 600s) |
| More than 50 monitors | `get_monitors` pages through the UptimeRobot API |
| Temporary network errors during polling | `on_error` logs them as warnings and polling continues |

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
| `WP_BOT_TOKEN`, `WP_URL`, `WP_POST_STATUS`, `WP_REFERENCE_URL`, `TMDB_API_KEY`, `WP_IMAGE_SIZE` | ➖ | see `README.md` |

---

## 🧩 Adding a Feature

1. Write an async function that takes `(update, context)`.
2. Decorate it with `@command("name", "Short description 💋")`. Menu descriptions must stay under 256 characters.
3. The command is added to the Telegram menu and `/help` automatically.
4. For long output, use `send_html()` so messages split safely at 3800 characters.
5. Keep the persona: English only, warm, flirty, and with emojis. Use `script()` for titles and `soft()` for labels (both from `ui.py`).

---

## ⚠️ Notes and Limitations

- Alert state is in memory only (resets on restart or redeploy). The same is true for open `/wp` and `/edit` sessions and the "Next episode" memory.
- The bot uses long polling, so run only **one instance** at a time.
- `drop_pending_updates=True` discards messages sent while the bot was offline.
- The startup message is retried on network errors. If the owner has never sent `/start`, Telegram refuses it and it is only logged.
- Render's free tier sleeps without traffic, so keep an UptimeRobot monitor pinging `/health`.
- On redeploy, the old and new instance can overlap briefly. Telegram may report a `Conflict` for a few seconds, which clears on its own.
- Unicode font styles can look different across devices, and screen readers may read them poorly.

---

## 🗺 Ideas for Later

- Persist alert state in a small database or file
- Alert on SSL expiry
- Per-monitor mute and snooze buttons
- Edit the post text and the SEO fields from `/edit`
- Weekly summary report 💌
