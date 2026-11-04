"""
NOONA 💋 Owner-only Telegram bot | UptimeRobot monitoring + daily report
Host: Render (Web Service) | Keep-alive: ping /health from UptimeRobot
See PROJECT.md for full documentation.
"""

import os
import asyncio
import time
import html
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
from aiohttp import web
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# =====================================================================
# FONTS (Telegram can't change typefaces, so we use two Unicode styles)
#   script() -> 𝓑𝓸𝓵𝓭 𝓢𝓬𝓻𝓲𝓹𝓽      for titles / headings
#   soft()   -> 𝙎𝙖𝙣𝙨 𝘽𝙤𝙡𝙙 𝙄𝙩𝙖𝙡𝙞𝙘  for labels / captions
# Only letters A-Z / a-z are converted; everything else is untouched.
# =====================================================================


def _restyle(s: str, upper_start: int, lower_start: int) -> str:
    out = []
    for ch in s:
        if "A" <= ch <= "Z":
            out.append(chr(upper_start + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(lower_start + ord(ch) - ord("a")))
        else:
            out.append(ch)
    return "".join(out)


def script(s: str) -> str:
    return _restyle(s, 0x1D4D0, 0x1D4EA)  # Mathematical Bold Script


def soft(s: str) -> str:
    return _restyle(s, 0x1D63C, 0x1D656)  # Mathematical Sans-Serif Bold Italic


# =====================================================================
# CONFIG
# =====================================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_KEY = os.environ["UPTIMEROBOT_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Kolkata"))
PORT = int(os.environ.get("PORT", "10000"))
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "60"))  # seconds
WELCOME_IMAGE = os.environ.get("WELCOME_IMAGE", "https://imgyx.pages.dev/ZkcBn")
NOTIFY_STARTUP = os.environ.get("NOTIFY_STARTUP", "1") == "1"

DENY_TEXT = "Sorry sweetheart 💋 my heart and my body belong to my owner only. Nice try though 😘"

START_TIME = time.time()
DIV = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"

STATUS = {
    0: "⏸ Paused",
    1: "⏳ Waiting",
    2: "💚 Up",
    8: "🧡 Looks down",
    9: "💔 Down",
}
DOWN_STATES = (8, 9)

# runtime state (memory only)
last_status: dict[int, int] = {}
down_since: dict[int, int] = {}

# command registry (menu + handlers are built from this)
COMMANDS: list[tuple[str, str]] = []
HANDLERS: list[CommandHandler] = []


def command(name: str, desc: str):
    """Decorator: register an owner-only command. Add new features with this."""

    def deco(fn):
        COMMANDS.append((name, desc))
        HANDLERS.append(CommandHandler(name, fn))
        return fn

    return deco


# =====================================================================
# HELPERS
# =====================================================================
def esc(s) -> str:
    return html.escape(str(s))


async def api(endpoint: str, **params) -> dict:
    params.update(api_key=API_KEY, format="json")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"https://api.uptimerobot.com/v2/{endpoint}", data=params)
            return r.json()
    except Exception as e:  # network / json errors
        log.warning("UptimeRobot API error: %s", e)
        return {"stat": "fail", "error": {"message": str(e)}}


def err(data: dict) -> str:
    e = data.get("error")
    if isinstance(e, dict):
        return e.get("message") or e.get("type") or "unknown error"
    return str(e)


def fmt_dur(sec) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_t(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%I:%M %p")


async def send_html(bot, chat_id: int, text: str):
    """Send long text safely (split on line boundaries, HTML mode)."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await bot.send_message(
                chat_id, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await bot.send_message(
            chat_id, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


# =====================================================================
# TEXT BUILDERS (used by commands AND buttons)
# =====================================================================
async def text_monitors() -> str:
    data = await api("getMonitors")
    if data.get("stat") != "ok":
        return f"💔 Oh no, darling: {esc(err(data))}"
    if not data["monitors"]:
        return "Nothing to watch yet, darling 🥺\nUse /add and give me something to adore 💞"

    lines = [f"📡 <b>{script('My Monitors')}</b>", DIV, ""]
    for m in data["monitors"]:
        lines.append(f"{STATUS.get(m['status'], m['status'])} <b>{esc(m['friendly_name'])}</b>")
        lines.append(f"🆔 <code>{m['id']}</code>")
        lines.append(f"🔗 {esc(m['url'])}")
        lines.append("")
    return "\n".join(lines)


async def text_status() -> str:
    data = await api("getMonitors")
    if data.get("stat") != "ok":
        return f"💔 Oh no, darling: {esc(err(data))}"
    ms = data["monitors"]
    up = sum(1 for m in ms if m["status"] == 2)
    paused = sum(1 for m in ms if m["status"] == 0)
    down = [m for m in ms if m["status"] in DOWN_STATES]

    lines = [
        f"📈 <b>{script('Status Report')}</b>",
        DIV,
        f"🌹 {soft('Total')}: {len(ms)}",
        f"💚 {soft('Up')}: {up}",
        f"💔 {soft('Down')}: {len(down)}",
        f"⏸ {soft('Paused')}: {paused}",
    ]
    if down:
        lines.append("")
        lines.append(f"🥀 <b>{soft('Hurting right now')}:</b>")
        for m in down:
            since = down_since.get(m["id"])
            extra = f" (for {fmt_dur(time.time() - since)})" if since else ""
            lines.append(f"• {esc(m['friendly_name'])}{extra}")
    else:
        lines.append("")
        lines.append("✨ Everything is perfect, baby. Not a single hiccup 😘")
    return "\n".join(lines)


async def build_report(start: int, end: int, title: str) -> str:
    data = await api(
        "getMonitors",
        logs=1,
        logs_start_date=start,
        logs_end_date=end,
        custom_uptime_ranges=f"{start}_{end}",
    )
    if data.get("stat") != "ok":
        return f"💔 UptimeRobot error: {esc(err(data))}"

    lines = [f"📊 <b>{script(title)}</b>", DIV, ""]
    total_incidents = 0
    for m in data["monitors"]:
        uptime = m.get("custom_uptime_ranges", "—")
        downs = [l for l in m.get("logs", []) if l["type"] == 1]
        total_incidents += len(downs)

        lines.append(f"💞 <b>{esc(m['friendly_name'])}</b> — {STATUS.get(m['status'], m['status'])}")
        lines.append(f"⏱ {soft('Uptime')}: {esc(uptime)}%")
        if not downs:
            lines.append("✅ No downtime at all. I kept it safe for you 💖")
        for idx, l in enumerate(downs):
            began = l["datetime"]
            back = began + l["duration"]
            if idx == 0 and m["status"] in DOWN_STATES:
                lines.append(f"💔 Down since {fmt_t(began)}, and still down. I'm worried 🥺")
            else:
                lines.append(
                    f"💔 Down {fmt_t(began)} → 💚 Up {fmt_t(back)} ({fmt_dur(l['duration'])})"
                )
        lines.append("")

    lines.append(f"🎀 {soft('Monitors')}: {len(data['monitors'])} | {soft('Incidents')}: {total_incidents}")
    return "\n".join(lines)


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📡 Monitors", callback_data="m:monitors"),
                InlineKeyboardButton("📈 Status", callback_data="m:status"),
            ],
            [
                InlineKeyboardButton("📊 Report", callback_data="m:report"),
                InlineKeyboardButton("💌 Help", callback_data="m:help"),
            ],
        ]
    )


def text_help() -> str:
    lines = [f"💌 <b>{script('What I Can Do For You')}</b>", DIV, ""]
    for name, desc in COMMANDS:
        lines.append(f"/{name} — {esc(desc)}")
    lines += [
        "",
        "🔔 I'll whisper to you the moment something goes down or comes back up, "
        "and send a daily report at 12:00 AM 🌙",
    ]
    return "\n".join(lines)


# =====================================================================
# SECURITY GUARD (runs before everything; blocks non-owners)
# =====================================================================
async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and user.id == OWNER_ID:
        return  # owner -> continue to handlers
    msg = update.effective_message
    if update.callback_query:
        await update.callback_query.answer(DENY_TEXT, show_alert=True)
    elif msg and update.effective_chat:
        is_cmd = bool(msg.text and msg.text.startswith("/"))
        if update.effective_chat.type == "private" or is_cmd:
            await msg.reply_text(DENY_TEXT)
    raise ApplicationHandlerStop


# =====================================================================
# COMMANDS (add new features below using @command)
# =====================================================================
@command("start", "Come say hello 💋")
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (
        f"💘 <b>{script('Welcome back, my love')}</b>\n"
        f"{DIV}\n\n"
        "I'm <b>Noona</b>, your devoted Uptime Guardian 🌹\n"
        "I'm watching every monitor for you, darling. The second one goes down, "
        "I'll whisper it in your ear 💌\n\n"
        "And every night at midnight, I'll send you a little report 🌙✨"
    )
    chat_id = update.effective_chat.id
    try:
        await context.bot.send_photo(
            chat_id,
            WELCOME_IMAGE,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=menu_keyboard(),
        )
    except Exception as e:
        log.warning("Welcome image failed: %s", e)
        await context.bot.send_message(
            chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=menu_keyboard()
        )


@command("help", "See everything I can do 💌")
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(context.bot, update.effective_chat.id, text_help())


@command("status", "Quick summary: up, down, paused 📈")
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(context.bot, update.effective_chat.id, await text_status())


@command("monitors", "All your monitors with IDs 📡")
async def monitors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(context.bot, update.effective_chat.id, await text_monitors())


@command("add", "Add a monitor: /add <name> <url> 💞")
async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text(
            "Tell me what to watch, darling 😘\n"
            "Use: /add <name> <url>\n"
            "Example: /add MyBot https://mybot.onrender.com"
        )
    url = context.args[-1]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    name = " ".join(context.args[:-1])
    data = await api("newMonitor", friendly_name=name, url=url, type=1, interval=300)
    if data.get("stat") == "ok":
        await update.message.reply_text(
            f"💞 Added {name}! I'll watch it closely for you.\n🆔 ID: {data['monitor']['id']}"
        )
    else:
        await update.message.reply_text(f"💔 Oh no, darling: {err(data)}")


@command("remove", "Delete a monitor: /remove <id> 🗑")
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text(
            "Use: /remove <id>\nYou can find the ID with /monitors 💋"
        )
    data = await api("deleteMonitor", id=context.args[0])
    await update.message.reply_text(
        "🗑 Done, it's gone. Goodbye, little monitor 💔"
        if data.get("stat") == "ok"
        else f"💔 Oh no, darling: {err(data)}"
    )


@command("pause", "Pause a monitor: /pause <id> ⏸")
async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Use: /pause <id>")
    data = await api("editMonitor", id=context.args[0], status=0)
    await update.message.reply_text(
        "⏸ Paused. Taking a little break together 😌"
        if data.get("stat") == "ok"
        else f"💔 Oh no, darling: {err(data)}"
    )


@command("resume", "Resume a monitor: /resume <id> ▶️")
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Use: /resume <id>")
    data = await api("editMonitor", id=context.args[0], status=1)
    await update.message.reply_text(
        "▶️ Resumed. Back to watching, just for you 😘"
        if data.get("stat") == "ok"
        else f"💔 Oh no, darling: {err(data)}"
    )


@command("report", "Report: /report [hours], default 24 📊")
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hours = 24
    if context.args and context.args[0].isdigit():
        hours = max(1, min(int(context.args[0]), 720))
    end = int(time.time())
    text = await build_report(end - hours * 3600, end, f"Report: last {hours}h")
    await send_html(context.bot, update.effective_chat.id, text)


@command("ping", "Check my speed 🏓")
async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t0 = time.perf_counter()
    data = await api("getAccountDetails")
    api_ms = int((time.perf_counter() - t0) * 1000)
    api_ok = "💚" if data.get("stat") == "ok" else "💔"
    await update.message.reply_text(
        f"🏓 Pong, handsome!\n"
        f"💞 I've been with you for {fmt_dur(time.time() - START_TIME)}\n"
        f"📡 UptimeRobot API: {api_ok} {api_ms} ms"
    )


# ---------------------------------------------------------------------
# Inline buttons
# ---------------------------------------------------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    key = q.data

    if key == "m:monitors":
        text = await text_monitors()
    elif key == "m:status":
        text = await text_status()
    elif key == "m:report":
        end = int(time.time())
        text = await build_report(end - 86400, end, "Report: last 24h")
    elif key == "m:help":
        text = text_help()
    else:
        return
    await send_html(context.bot, chat_id, text)


# =====================================================================
# BACKGROUND JOBS
# =====================================================================
async def check_job(context: ContextTypes.DEFAULT_TYPE):
    data = await api("getMonitors")
    if data.get("stat") != "ok":
        log.warning("getMonitors failed: %s", data)
        return

    now = int(time.time())
    first_run = not last_status

    for m in data["monitors"]:
        mid, status, name = m["id"], m["status"], esc(m["friendly_name"])
        prev = last_status.get(mid)
        last_status[mid] = status

        if first_run or prev is None or prev == status:
            if status in DOWN_STATES and mid not in down_since:
                down_since[mid] = now
            continue

        if status in DOWN_STATES and prev not in DOWN_STATES:
            down_since[mid] = now
            await context.bot.send_message(
                OWNER_ID,
                f"💔 <b>{script('Down')}</b>: {name}\n"
                f"🔗 {esc(m['url'])}\n"
                f"🕒 {fmt_t(now)}\n\n"
                "Don't worry, darling. I'm keeping an eye on it for you 🥺",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        elif status == 2 and prev in DOWN_STATES:
            started = down_since.pop(mid, now)
            await context.bot.send_message(
                OWNER_ID,
                f"💚 <b>{script('Back Up')}</b>: {name}\n"
                f"⏱ It was down for {fmt_dur(now - started)}\n"
                f"🌅 Offline {fmt_t(started)} → Online {fmt_t(now)}\n\n"
                "All better now, baby 😘",
                parse_mode=ParseMode.HTML,
            )

    live = {m["id"] for m in data["monitors"]}
    for mid in list(last_status):
        if mid not in live:
            last_status.pop(mid, None)
            down_since.pop(mid, None)


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)
    end = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    start = end - 86400
    day = datetime.fromtimestamp(start, TZ).strftime("%d %b %Y")
    text = await build_report(start, end, f"Daily Report ({day})")
    await send_html(context.bot, OWNER_ID, text)


# =====================================================================
# WEB SERVER (Render needs a port; UptimeRobot pings this to keep alive)
# =====================================================================
async def start_web():
    async def health(_):
        return web.json_response(
            {"status": "ok", "bot_uptime_sec": int(time.time() - START_TIME)}
        )

    app = web.Application()
    app.add_routes([web.get("/", health), web.get("/health", health), web.get("/ping", health)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health server on port %s", PORT)


async def post_init(app: Application):
    await start_web()
    try:
        await app.bot.set_my_commands(
            [BotCommand(n, d[:250]) for n, d in COMMANDS],
            scope=BotCommandScopeChat(OWNER_ID),  # menu only for owner
        )
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)
    if NOTIFY_STARTUP:
        try:
            await app.bot.send_message(
                OWNER_ID, "💋 I'm online and all yours again (deploy/restart) 💞"
            )
        except Exception as e:
            log.warning("Startup notify failed (has the owner sent /start yet?): %s", e)


# =====================================================================
# MAIN
# =====================================================================
def main():
    # Python 3.14 fix: the main thread has no default event loop
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)  # owner-only gate
    for h in HANDLERS:
        app.add_handler(h)
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^m:"))
    app.job_queue.run_repeating(check_job, interval=CHECK_EVERY, first=5)
    app.job_queue.run_daily(daily_job, time=dtime(0, 0, tzinfo=TZ))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
