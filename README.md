# 💋 NOONA

> Your devoted Uptime Guardian. She watches your websites, whispers when something goes down, and sends you a little report every night. 🌙

NOONA is an **owner-only Telegram bot** that connects to [UptimeRobot](https://uptimerobot.com), alerts you when a monitor goes down or comes back up, and sends a daily report. She is flirty, romantic, and completely loyal to her owner. 💞

---

## ✨ Features

- 🔔 **Instant alerts** when a monitor goes down or recovers
- 📊 **Daily report** at 12:00 AM (your timezone)
- 📡 **Manage monitors** from chat: add, remove, pause, resume
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

---

## 🚀 Setup

### 1. Get your keys 🔑

- **Bot token**: create a bot with [@BotFather](https://t.me/BotFather)
- **UptimeRobot API key**: UptimeRobot dashboard → Integrations → API (use the **main** API key)
- **Your Telegram user ID**: message [@userinfobot](https://t.me/userinfobot)

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
| `bot.py` | The whole bot |
| `requirements.txt` | Python dependencies |
| `PROJECT.md` | Technical documentation |
| `README.md` | You are here 💞 |

---

Made with 💖 for one owner only.
