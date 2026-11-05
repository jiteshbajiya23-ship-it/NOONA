"""
Owner-only Telegram bot | UptimeRobot monitoring + daily report
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
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# FIX: httpx logs full request URLs at INFO, which leaks the bot token
# (https://api.telegram.org/bot<TOKEN>/getUpdates) into Render logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bot")

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

DENY_TEXT = "I Gave My Body To My Owner"
START_TIME = time.time()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

STATUS = {0: "⏸ Paused", 1: "⏳ Not checked", 2: "🟢 Up", 8: "🟠 Seems down", 9: "🔴 Down"}
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


async def get_monitors(**params) -> dict:
    """getMonitors with pagination (API returns max 50 per page)."""
    out: list = []
    offset = 0
    while True:
        data = await api("getMonitors", offset=offset, limit=50, **params)
        if data.get("stat") != "ok":
            return data
        page = data.get("monitors", [])
        out.extend(page)
        total = int((data.get("pagination") or {}).get("total", len(out)))
        if not page or len(out) >= total:
            break
        offset = len(out)
    data["monitors"] = out
    return data


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


def fmt_t(ts: int, with_date: bool = False) -> str:
    fmt = "%d %b %I:%M %p" if with_date else "%I:%M %p"
    return datetime.fromtimestamp(ts, TZ).strftime(fmt)


async def retry_tg(fn, *args, tries: int = 5, delay: float = 3, label: str = "", **kwargs):
    """Retry a Telegram call on timeouts / network errors. Never raises."""
    for i in range(1, tries + 1):
        try:
            return await fn(*args, **kwargs)
        except NetworkError as e:  # includes TimedOut
            log.warning("%s attempt %d/%d failed: %s", label or "telegram call", i, tries, e)
            await asyncio.sleep(delay * i)
        except Exception as e:  # Forbidden, BadRequest etc. -> no point retrying
            log.warning("%s failed: %s", label or "telegram call", e)
            return None
    return None


async def send_html(bot, chat_id: int, text: str):
    """Send long text safely (split on line boundaries, HTML mode)."""
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > 3800:
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML,
                                   link_preview_options=NO_PREVIEW)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML,
                               link_preview_options=NO_PREVIEW)


# =====================================================================
# TEXT BUILDERS (used by commands AND buttons)
# =====================================================================
async def text_monitors() -> str:
    data = await get_monitors()
    if data.get("stat") != "ok":
        return f"❌ {esc(err(data))}"
    if not data["monitors"]:
        return "Koi monitor nahi hai. /add se add karo."
    lines = ["<b>📡 Monitors</b>", ""]
    for m in data["monitors"]:
        lines.append(f"{STATUS.get(m['status'], m['status'])} <b>{esc(m['friendly_name'])}</b>")
        lines.append(f"ID: <code>{m['id']}</code>")
        lines.append(f"{esc(m.get('url', ''))}")
        lines.append("")
    return "\n".join(lines)


async def text_status() -> str:
    data = await get_monitors()
    if data.get("stat") != "ok":
        return f"❌ {esc(err(data))}"
    ms = data["monitors"]
    up = sum(1 for m in ms if m["status"] == 2)
    paused = sum(1 for m in ms if m["status"] == 0)
    down = [m for m in ms if m["status"] in DOWN_STATES]
    lines = [
        "<b>📈 Status</b>",
        f"Total: {len(ms)}",
        f"🟢 Up: {up}",
        f"🔴 Down: {len(down)}",
        f"⏸ Paused: {paused}",
    ]
    if down:
        lines.append("")
        lines.append("<b>Abhi down:</b>")
        for m in down:
            since = down_since.get(m["id"])
            extra = f" ({fmt_dur(time.time() - since)} se)" if since else ""
            lines.append(f"• {esc(m['friendly_name'])}{extra}")
    else:
        lines.append("\n✅ Sab kuch theek chal raha hai")
    return "\n".join(lines)


async def build_report(start: int, end: int, title: str) -> str:
    data = await get_monitors(
        logs=1,
        logs_start_date=start,
        logs_end_date=end,
        custom_uptime_ranges=f"{start}_{end}",
    )
    if data.get("stat") != "ok":
        return f"❌ UptimeRobot error: {esc(err(data))}"

    wd = (end - start) > 86400  # show dates for reports longer than 1 day
    lines = [f"<b>📊 {esc(title)}</b>", ""]
    total_incidents = 0
    for m in data["monitors"]:
        uptime = m.get("custom_uptime_ranges", "—")
        downs = [l for l in m.get("logs", []) if l["type"] == 1]
        total_incidents += len(downs)
        lines.append(f"<b>{esc(m['friendly_name'])}</b> — {STATUS.get(m['status'], m['status'])}")
        lines.append(f"Uptime: {esc(uptime)}%")
        if not downs:
            lines.append("✅ Koi downtime nahi")
        for idx, l in enumerate(downs):
            began = l["datetime"]
            back = began + l["duration"]
            if idx == 0 and m["status"] in DOWN_STATES:
                lines.append(f"🔴 {fmt_t(began, wd)} se down — abhi bhi down hai")
            else:
                lines.append(
                    f"🔴 Down {fmt_t(began, wd)} → 🟢 Up {fmt_t(back, wd)} ({fmt_dur(l['duration'])})"
                )
        lines.append("")
    lines.append(f"Monitors: {len(data['monitors'])} | Incidents: {total_incidents}")
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
                InlineKeyboardButton("❓ Help", callback_data="m:help"),
            ],
        ]
    )


def text_help() -> str:
    lines = ["<b>🛠 Commands</b>", ""]
    for name, desc in COMMANDS:
        lines.append(f"/{name} — {esc(desc)}")
    lines += ["", "🔔 Auto: down/up alerts + daily report 12:00 AM"]
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
@command("start", "Bot start + menu")
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (
        "👑 <b>Welcome back, Owner!</b>\n\n"
        "Main tumhara Uptime Guardian hoon.\n"
        "Har monitor pe nazar hai — down hote hi alert, "
        "aur roz 12:00 AM report. 🚀"
    )
    chat_id = update.effective_chat.id
    try:
        await context.bot.send_photo(
            chat_id, WELCOME_IMAGE, caption=caption,
            parse_mode=ParseMode.HTML, reply_markup=menu_keyboard(),
        )
    except Exception as e:
        log.warning("Welcome image failed: %s", e)
        await context.bot.send_message(
            chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=menu_keyboard()
        )


@command("help", "Saare commands")
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(context.bot, update.effective_chat.id, text_help())


@command("status", "Quick summary (up/down/paused)")
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(context.bot, update.effective_chat.id, await text_status())


@command("monitors", "Saare monitors + ID")
async def monitors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(context.bot, update.effective_chat.id, await text_monitors())


@command("add", "Monitor add: /add <name> <url>")
async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text(
            "Use: /add <name> <url>\nEx: /add MyBot https://mybot.onrender.com"
        )
    url = context.args[-1]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    name = " ".join(context.args[:-1])
    data = await api("newMonitor", friendly_name=name, url=url, type=1, interval=300)
    if data.get("stat") == "ok":
        await update.message.reply_text(f"✅ Added: {name}\nID: {data['monitor']['id']}")
    else:
        await update.message.reply_text(f"❌ {err(data)}")


@command("remove", "Monitor delete: /remove <id>")
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Use: /remove <id>\nID /monitors se dekho.")
    data = await api("deleteMonitor", id=context.args[0])
    await update.message.reply_text("🗑 Monitor removed." if data.get("stat") == "ok"
                                    else f"❌ {err(data)}")


@command("pause", "Monitor pause: /pause <id>")
async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Use: /pause <id>")
    data = await api("editMonitor", id=context.args[0], status=0)
    await update.message.reply_text("⏸ Paused." if data.get("stat") == "ok"
                                    else f"❌ {err(data)}")


@command("resume", "Monitor resume: /resume <id>")
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Use: /resume <id>")
    data = await api("editMonitor", id=context.args[0], status=1)
    await update.message.reply_text("▶️ Resumed." if data.get("stat") == "ok"
                                    else f"❌ {err(data)}")


@command("report", "Report: /report [hours] (default 24)")
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hours = 24
    if context.args and context.args[0].isdigit():
        hours = max(1, min(int(context.args[0]), 720))
    end = int(time.time())
    text = await build_report(end - hours * 3600, end, f"Report — last {hours}h")
    await send_html(context.bot, update.effective_chat.id, text)


@command("ping", "Bot + API speed check")
async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t0 = time.perf_counter()
    data = await api("getAccountDetails")
    api_ms = int((time.perf_counter() - t0) * 1000)
    api_ok = "✅" if data.get("stat") == "ok" else "❌"
    await update.message.reply_text(
        f"🏓 Pong!\nBot uptime: {fmt_dur(time.time() - START_TIME)}\n"
        f"UptimeRobot API: {api_ok} {api_ms} ms"
    )


# ---------------------------------------------------------------------
# Inline buttons
# ---------------------------------------------------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id if q.message else q.from_user.id
    key = q.data

    if key == "m:monitors":
        text = await text_monitors()
    elif key == "m:status":
        text = await text_status()
    elif key == "m:report":
        end = int(time.time())
        text = await build_report(end - 86400, end, "Report — last 24h")
    elif key == "m:help":
        text = text_help()
    else:
        return
    await send_html(context.bot, chat_id, text)


# =====================================================================
# BACKGROUND JOBS
# =====================================================================
async def startup_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs once after start. Retries so Telegram timeouts on cold start don't matter."""
    ok = await retry_tg(
        context.bot.set_my_commands,
        [BotCommand(n, d[:250]) for n, d in COMMANDS],
        scope=BotCommandScopeChat(OWNER_ID),  # menu only for owner
        label="set_my_commands",
    )
    if ok:
        log.info("Command menu set")
    if NOTIFY_STARTUP:
        sent = await retry_tg(
            context.bot.send_message, OWNER_ID,
            "✅ Bot online ho gaya (deploy/restart).",
            label="startup notify",
        )
        if sent is None:
            log.warning("Startup notify not delivered (owner ne /start nahi kiya ya network issue)")


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    data = await get_monitors()
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

        try:
            if status in DOWN_STATES and prev not in DOWN_STATES:
                down_since[mid] = now
                await context.bot.send_message(
                    OWNER_ID,
                    f"🔴 <b>DOWN</b>: {name}\n{esc(m.get('url', ''))}\n🕒 {fmt_t(now)}",
                    parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                )
            elif status == 2 and prev in DOWN_STATES:
                started = down_since.pop(mid, now)
                await context.bot.send_message(
                    OWNER_ID,
                    f"🟢 <b>BACK UP</b>: {name}\n"
                    f"⏱ Down tha: {fmt_dur(now - started)}\n"
                    f"Offline {fmt_t(started)} → Online {fmt_t(now)}",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:  # one failed alert must not break the others
            log.warning("Alert send failed for %s: %s", mid, e)

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
    try:
        await send_html(context.bot, OWNER_ID, text)
    except Exception as e:
        log.warning("Daily report send failed: %s", e)


# =====================================================================
# ERROR HANDLER
# =====================================================================
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    e = context.error
    if isinstance(e, NetworkError):
        log.warning("Network error (will retry automatically): %s", e)
    else:
        log.error("Unhandled error: %s", e, exc_info=e)


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


# =====================================================================
# MAIN
# =====================================================================
def main():
    # Python 3.14 fix: main thread me default event loop nahi hota
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        # FIX: default timeouts are 5s -> "Timed out" on Render cold start
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(20)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(20)
        .get_updates_pool_timeout(20)
        .build()
    )

    app.add_handler(TypeHandler(Update, guard), group=-1)  # owner-only gate
    for h in HANDLERS:
        app.add_handler(h)
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^m:"))
    app.add_error_handler(on_error)

    app.job_queue.run_once(startup_job, when=3)
    app.job_queue.run_repeating(check_job, interval=CHECK_EVERY, first=5)
    app.job_queue.run_daily(daily_job, time=dtime(0, 0, tzinfo=TZ))

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
