# 💋 NOONA

> Your devoted guardian. She watches your websites, whispers when something goes down, posts your episodes, and sends you a little report every night. 🌙

NOONA is an **owner-only Telegram bot**. She connects to [UptimeRobot](https://uptimerobot.com) to alert you when a monitor goes down or comes back up, and to your **WordPress site** so you can create and edit episode posts from chat, all with buttons. She is flirty, romantic, and completely loyal to her owner. 💞

---

## ✨ Features

- 🔔 **Instant alerts** when a monitor goes down or recovers
- 📊 **Daily report** at 12:00 AM (your timezone)
- 📡 **Manage monitors** from chat: add, remove, pause, resume
- 🕹 **Interactive buttons**: menu, alert buttons (🔎 Check now · ⏸ Pause it · 💖 Got it), and a button flow for every post
- 📝 **Website posts**: series, category, episode, embeds and TMDB poster, all picked with taps
- ✏️ **Edit old posts**: change the title, episode, embeds, poster, category, or publish/draft status any time
- ⏭ **Next episode** button: same series, category and poster, episode + 1, in one tap
- 🔐 **Owner-only**: anyone else gets politely rejected 😘
- 💓 **Health endpoint** (`/health`) so UptimeRobot can keep her awake on Render
- 🎨 **Pretty UI** with emojis, dividers, and two Unicode font styles (script for titles, bold italic for labels)

---

## 💌 Commands

| Command | What it does |
|---|---|
| `/start` | Say hello and open the menu 💋 |
| `/help` | See everything she can do 💌 |
| `/status` | Quick summary: up, down, paused 📈 |
| `/monitors` | All monitors with their IDs 📡 |
| `/add <name> <url>` | Add a monitor 💞 |
| `/remove <id>` | Delete a monitor 🗑 |
| `/pause <id>` | Pause a monitor ⏸ |
| `/resume <id>` | Resume a monitor ▶️ |
| `/report [hours]` | Report for the last N hours (default 24, max 720) 📊 |
| `/ping` | Check bot uptime and API speed 🏓 |
| `/wp [name]` | Create a website episode post (series, category, episode, embeds, featured image) 📝 |
| `/edit [link, id or name]` | Edit an old post. Examples: `/edit Soul Land 2 ep 14`, `/edit 1234`, `/edit https://site/post/` ✏️ |
| `/wpcheck` | Test the connection to the website plugin 🔌 |
| `/cancel` | Stop the current `/wp` or `/edit` flow ✖️ |

---

## 🚀 Setup

### 1. Get your keys 🔑

- **Bot token**: create a bot with [@BotFather](https://t.me/BotFather)
- **UptimeRobot API key**: UptimeRobot dashboard → Integrations → API (use the **main** API key)
- **Your Telegram user ID**: message [@userinfobot](https://t.me/userinfobot)
- **TMDB API key** (optional, for posters): themoviedb.org → Settings → API
- **Website secret** (optional, for posting/editing): any random string of 20+ characters you make up yourself

> ℹ️ NOONA is a normal **bot**, so she needs **no Telegram `api_id` / `api_hash`**. Those are only for user-account libraries (Telethon, Pyrogram), which this project does not use.

### 2. Install 📦

```bash
git clone https://github.com/jiteshbajiya23-ship-it/NOONA.git
cd NOONA
pip install -r requirements.txt
```

### 3. Configure ⚙️

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | n/a | Telegram bot token |
| `UPTIMEROBOT_API_KEY` | ✅ | n/a | UptimeRobot main API key |
| `OWNER_ID` | ✅ | n/a | Your numeric Telegram user ID |
| `TZ_NAME` | ❌ | `Asia/Kolkata` | Timezone for times and the daily report |
| `PORT` | ❌ | `10000` | Port for the health server |
| `CHECK_EVERY` | ❌ | `60` | Seconds between status checks |
| `WELCOME_IMAGE` | ❌ | built-in URL | Image shown on `/start` |
| `NOTIFY_STARTUP` | ❌ | `1` | Set `0` to skip the "I'm online" message |
| `WP_BOT_TOKEN` | ➖ | n/a | Website posting: the secret inside `wordpress/xiaobhai-episode-api.php` (20+ chars) |
| `WP_URL` | ➖ | `https://xiaobhaidonghua.com` | Your WordPress site |
| `WP_POST_STATUS` | ➖ | `publish` | `publish` (live) or `draft` |
| `WP_REFERENCE_URL` | ➖ | Jade Dynasty ep 14 | An existing episode post **with an embed**; `/wpcheck` learns the theme's fields from it |
| `TMDB_API_KEY` | ➖ | n/a | Needed for the featured-image step of `/wp` (v3 key or v4 token) |
| `WP_IMAGE_SIZE` | ➖ | `w780` | TMDB size of the featured image: `w500`, `w780`, `original` |

**Website setup (one time, and again after every plugin update):**

1. Make up a long random secret (20+ characters).
2. Add this line to `wp-config.php` on the site: `define('XB_BOT_TOKEN', 'your-secret');`
   (the plugin ships with the placeholder `CHANGE_ME`, so it stays **locked** until you do this. Never commit the real secret to GitHub.)
3. Upload `wordpress/xiaobhai-episode-api.php` (**version 1.2**, needed for `/edit`) to `wp-content/mu-plugins/`.
4. Set `WP_BOT_TOKEN` to the same secret in Render and redeploy.
5. Send `/wpcheck`. Logic lives in `wp.py`.

### 4. Run ▶️

```bash
export BOT_TOKEN="..."
export UPTIMEROBOT_API_KEY="..."
export OWNER_ID="123456789"
python bot.py
```

Then open Telegram and send `/start` to your bot. 💘

---

## ☁️ Deploy on Render

1. Create a **Web Service** from this repo
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `python bot.py`
4. Add the environment variables above
5. In UptimeRobot, add an HTTP monitor pointing at `https://<your-service>.onrender.com/health` so the service stays awake 💓

---

## 🧩 Adding your own command

Use the `@command` decorator in `bot.py`. It registers the handler **and** the Telegram menu entry automatically:

```python
@command("hello", "Say hi to Noona 💋")
async def hello_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi, handsome 😘")
```

---

## 📁 Project files

| File | Purpose |
|---|---|
| `bot.py` | Monitoring, alerts, reports, menu |
| `wp.py` | Website posting and editing (`/wp`, `/edit`, `/wpcheck`) |
| `ui.py` | The two fonts and the romantic persona lines |
| `wordpress/xiaobhai-episode-api.php` | WordPress plugin (upload to `mu-plugins/`) |
| `requirements.txt` | Python dependencies |
| `PROJECT.md` | Technical documentation |
| `README.md` | You are here 💞 |

---

Made with 💖 for one owner only.
