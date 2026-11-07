"""
NOONA add-on: website posting (WordPress). Owner-only, one self-contained file.

Commands : /wp [name]  - create an episode post step by step
           /wpcheck    - test the connection to the website plugin
           /cancel     - stop the /wp flow
Flow     : name -> series -> category -> episode -> embeds -> featured image (TMDB) -> post

Install  : see the 3-line change in bot.py (import wp / wp.setup(app, COMMANDS) / await wp.close()).
Env vars : WP_BOT_TOKEN (required)  WP_URL  WP_POST_STATUS  WP_REFERENCE_URL
           TMDB_API_KEY  TMDB_LANGUAGE  WP_IMAGE_SIZE  IMPERSONATE
Server   : wordpress/xiaobhai-episode-api.php must sit in wp-content/mu-plugins/ on the website.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import urlparse

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("wp")

# =====================================================================
# CONFIG
# =====================================================================
WP_URL = os.environ.get("WP_URL", "https://xiaobhaidonghua.com").strip().rstrip("/")
WP_BOT_TOKEN = os.environ.get("WP_BOT_TOKEN", "").strip()  # same secret as inside the plugin file
WP_POST_STATUS = "draft" if os.environ.get("WP_POST_STATUS", "publish").strip().lower() == "draft" else "publish"
WP_REFERENCE_URL = os.environ.get(
    "WP_REFERENCE_URL",
    "https://xiaobhaidonghua.com/jade-dynasty-season-4-episode-14-english-subtitles/",
).strip()
WP_IMPERSONATE = os.environ.get("IMPERSONATE", "chrome124").strip() or "chrome124"
WP_IMAGE_SIZE = os.environ.get("WP_IMAGE_SIZE", "w780").strip() or "w780"
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()  # v3 key OR v4 "Read Access Token"
TMDB_LANGUAGE = os.environ.get("TMDB_LANGUAGE", "en-US").strip() or "en-US"
OWNER_ID = int(os.environ["OWNER_ID"])

HTML = ParseMode.HTML
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
TTL = 1800          # a /wp flow is dropped after 30 idle minutes
MAX_EMBEDS = 10
MAX_CODE = 4000


# =====================================================================
# SMALL HELPERS
# =====================================================================
def esc(v) -> str:
    return html.escape(str(v or ""), quote=False)


def short(text: str, limit: int = 30) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def markup(rows) -> InlineKeyboardMarkup | None:
    """rows = [[(label, "cb"|"url", value), ...], ...]"""
    if not rows:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(l, url=v) if k == "url" else InlineKeyboardButton(l, callback_data=v)
         for l, k, v in row]
        for row in rows
    ])


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("’", "").replace("'", "")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


_SEASON_TAIL = re.compile(r"\s+(?:season\s*\d+|s\d+|part\s*\d+|\d+(?:st|nd|rd|th)\s+season)$")


def strip_season(norm: str) -> str:
    return _SEASON_TAIL.sub("", norm).strip()


def similarity(a: str, b: str) -> float:
    """0-100, order-insensitive."""
    if a == b:
        return 100.0
    prefix_bonus = 70.0 if len(a) >= 4 and b.startswith(a + " ") else 0.0
    sm = SequenceMatcher(None, a, b)
    if sm.real_quick_ratio() < 0.45 or sm.quick_ratio() < 0.45:
        return prefix_bonus
    plain = sm.ratio()
    ta, tb = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    token = SequenceMatcher(None, ta, tb).ratio() if (ta != a or tb != b) else plain
    return max(100.0 * max(plain, token), prefix_bonus)


async def _reply(msg, text: str, rows=None):
    return await msg.reply_text(text, parse_mode=HTML, reply_markup=markup(rows),
                                link_preview_options=NO_PREVIEW)


async def _edit(msg, text: str, rows=None) -> None:
    """Edit a message whether it is a photo (caption) or plain text."""
    try:
        if msg.photo:
            await msg.edit_caption(text, parse_mode=HTML, reply_markup=markup(rows))
        else:
            await msg.edit_text(text, parse_mode=HTML, reply_markup=markup(rows),
                                link_preview_options=NO_PREVIEW)
    except Exception as exc:
        log.debug("edit failed (%s)", exc)


# =====================================================================
# WORDPRESS CLIENT (talks to wordpress/xiaobhai-episode-api.php)
# =====================================================================
class WPError(Exception):
    pass


def episode_title(name: str, season, episode: int, with_season: bool = True) -> str:
    """'Jade Dynasty Season 4 Episode 14 English Subtitles'."""
    parts = [" ".join(str(name or "").split())]
    if with_season and season:
        parts.append(f"Season {int(season)}")
    parts.append(f"Episode {int(episode)}")
    parts.append("English Subtitles")
    return " ".join(p for p in parts if p)


def slug_from_url(url: str) -> str:
    path = urlparse((url or "").strip()).path
    return [seg for seg in path.split("/") if seg][-1] if path.strip("/") else ""


def episode_from_slug(slug: str) -> int | None:
    m = re.search(r"episode-(\d+)", slug or "")
    return int(m.group(1)) if m else None


def _snippet(text: str, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()[:limit]


class WordPress:
    def __init__(self):
        self._session = None

    @property
    def enabled(self) -> bool:
        return bool(WP_URL and WP_BOT_TOKEN)

    async def _get_session(self):
        if self._session is None:
            try:  # real Chrome fingerprint, so basic firewalls do not block the bot
                from curl_cffi.requests import AsyncSession
                self._session = AsyncSession(impersonate=WP_IMPERSONATE, timeout=60)
            except ImportError:
                log.warning("curl_cffi not installed, using httpx (a firewall may block it)")
                self._session = httpx.AsyncClient(timeout=60, follow_redirects=True)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            try:
                res = self._session.aclose() if isinstance(self._session, httpx.AsyncClient) \
                    else self._session.close()
                await res
            except Exception:
                pass
            self._session = None

    @staticmethod
    def _candidates(route: str):
        yield f"{WP_URL}/wp-json/xiaobhai/v1{route}", {}
        yield f"{WP_URL}/", {"rest_route": f"/xiaobhai/v1{route}"}
        yield f"{WP_URL}/", {"xb_api": route}  # works even when a plugin restricts /wp-json/

    @staticmethod
    def _json(resp):
        try:
            data = resp.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    async def _call(self, method: str, route: str, params: dict | None = None, body: dict | None = None) -> dict:
        if not self.enabled:
            raise WPError("WP_BOT_TOKEN is not set.")
        session = await self._get_session()
        headers = {"X-XB-Token": WP_BOT_TOKEN, "Accept": "application/json", "Referer": WP_URL + "/"}
        errors: list[str] = []
        for url, extra in self._candidates(route):
            way = "wp-json" if "/wp-json/" in url else ("rest_route" if "rest_route" in extra else "xb_api")
            query = {**(params or {}), **extra, "token": WP_BOT_TOKEN}
            try:
                resp = await session.request(method, url, params=query or None, json=body, headers=headers)
            except Exception as exc:
                errors.append(f"[{way}] network error: {exc!r}"[:200])
                continue
            data = self._json(resp)
            status = resp.status_code
            if data is not None and status < 400:
                return data
            if data is not None and data.get("code"):
                if str(data["code"]).startswith("xb_"):  # the plugin itself answered (bad token, bad input...)
                    raise WPError(f"{data.get('message') or data['code']} (HTTP {status})")
                errors.append(f"[{way}] {data.get('message') or data['code']} (HTTP {status})"[:200])
                continue
            errors.append(f"[{way}] HTTP {status} - not the plugin's answer, probably a firewall/cache page: "
                          f"{_snippet(getattr(resp, 'text', ''), 60)}")
        raise WPError("\n".join(errors) or "no answer")

    async def ping(self) -> dict:
        return await self._call("GET", "/ping")

    async def series(self, search: str = "") -> list[dict]:
        return (await self._call("GET", "/series", {"search": search})).get("items") or []

    async def categories(self, search: str = "") -> list[dict]:
        return (await self._call("GET", "/categories", {"search": search})).get("items") or []

    async def calibrate(self, slug: str, episode: int) -> dict:
        return await self._call("POST", "/calibrate", body={"slug": slug, "episode": int(episode)})

    async def create_episode(self, *, title: str, episode: int, series_id: int,
                             category_ids: list[int] | None = None, status: str | None = None,
                             embeds: list[dict] | None = None, featured_image_url: str = "",
                             image_title: str = "") -> dict:
        body = {
            "title": title,
            "episode": int(episode),
            "series_id": int(series_id),
            "category_ids": [int(c) for c in (category_ids or [])],
            "status": status or WP_POST_STATUS,
            "embeds": [{"host": e["host"], "code": e["code"]} for e in (embeds or [])],
        }
        if featured_image_url:
            body["featured_image_url"] = featured_image_url
            body["image_title"] = image_title or title
        return await self._call("POST", "/episode", body=body)


wp = WordPress()


# =====================================================================
# TMDB CLIENT (only what the featured-image step needs)
# =====================================================================
class TMDBError(Exception):
    pass


TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/"
_TV_URL = re.compile(r"themoviedb\.org/tv/(\d+)", re.I)


def image_url(path: str | None, size: str = "w780") -> str:
    return f"{TMDB_IMG}{size}{path}" if path else ""


def parse_tv_id(text: str) -> int | None:
    text = (text or "").strip()
    m = _TV_URL.search(text)
    if m:
        return int(m.group(1))
    return int(text) if text.isdigit() else None


class TMDB:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(TMDB_API_KEY)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, **params):
        if not self.enabled:
            raise TMDBError("TMDB_API_KEY is not set")
        headers = {"accept": "application/json"}
        params.setdefault("language", TMDB_LANGUAGE)
        if params["language"] is None:
            del params["language"]
        if TMDB_API_KEY.startswith("eyJ"):  # v4 token (JWT)
            headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
        else:
            params["api_key"] = TMDB_API_KEY
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20)
        last = None
        for attempt in range(3):
            try:
                r = await self._client.get(f"{TMDB_BASE}{path}", params=params, headers=headers)
                if r.status_code == 401:
                    raise TMDBError("TMDB rejected the API key (401). Check TMDB_API_KEY.")
                if r.status_code == 404:
                    raise TMDBError(f"TMDB: not found ({path})")
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                    await asyncio.sleep(min(2.0 * (attempt + 1), 30))
                    continue
                if r.status_code >= 400:
                    raise TMDBError(f"TMDB answered HTTP {r.status_code}")
                return r.json()
            except httpx.HTTPError as exc:
                last = repr(exc)
                await asyncio.sleep(1 + attempt)
        raise TMDBError(f"TMDB request failed: {last}")

    async def search_tv(self, query: str) -> list[dict]:
        return (await self._get("/search/tv", query=query, include_adult="false")).get("results") or []

    async def tv(self, tv_id: int) -> dict:
        return await self._get(f"/tv/{int(tv_id)}")

    async def all_posters(self, tv_id: int) -> list[dict]:
        data = await self._get(f"/tv/{int(tv_id)}/images", language=None)
        posters = [p for p in (data.get("posters") or []) if p.get("file_path")]
        posters.sort(key=lambda p: (-(p.get("vote_average") or 0), -(p.get("vote_count") or 0)))
        return posters


tmdb = TMDB()


# =====================================================================
# FLOW STATE (one Flow per owner, in memory)
# =====================================================================
@dataclass
class Flow:
    user_id: int
    step: str = "name"
    name: str = ""
    series: list = field(default_factory=list)
    series_id: int = 0
    series_title: str = ""
    cats: list = field(default_factory=list)
    cat_id: int = 0
    cat_title: str = ""
    episode: int = 0
    embeds: list = field(default_factory=list)  # [{"host": ..., "code": ...}]
    host: str = ""
    tmdb_id: int = 0
    tmdb_name: str = ""
    posters: list = field(default_factory=list)
    pos: int = 0
    image_path: str = ""
    ts: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.ts = time.monotonic()


_flows: dict[int, Flow] = {}
TEXT_STEPS = {"name", "series_q", "cat_q", "episode", "emb_host", "emb_code", "tmdb_q"}


def flow_start(user_id: int, name: str = "") -> Flow:
    flow = Flow(user_id=user_id, name=(name or "").strip())
    _flows[user_id] = flow
    return flow


def flow_get(user_id: int) -> Flow | None:
    flow = _flows.get(user_id)
    if flow and time.monotonic() - flow.ts > TTL:
        _flows.pop(user_id, None)
        return None
    return flow


def flow_drop(user_id: int) -> bool:
    return _flows.pop(user_id, None) is not None


# =====================================================================
# SEARCH LOGIC (series / categories / TMDB show)
# =====================================================================
def search_terms(name: str) -> list[str]:
    """'Soul Land 2 Season 3' -> full name, name without season, first two words, first word."""
    name = " ".join((name or "").split())
    words = name.split()
    plain = re.sub(r"\s+(?:season\s*\d+|s\d+|part\s*\d+)$", "", name, flags=re.I).strip()
    out: list[str] = []
    for q in (name, plain, " ".join(plain.split()[:2]), words[0] if words and len(words[0]) > 3 else ""):
        q = q.strip()
        if q and q.lower() not in [o.lower() for o in out]:
            out.append(q)
    return out


def _match(q: str, t: str) -> float:
    if not q or not t:
        return 0.0
    if q == t:
        return 100.0
    if t.startswith(q + " "):
        return 92.0
    return min(similarity(q, t), 90.0)


def rank_items(items: list[dict], query: str, limit: int = 8) -> list[dict]:
    q = normalize(query)
    base = strip_season(q)

    def score(item):
        t = normalize(item.get("title"))
        return max(_match(q, t), _match(base, t) - 3)

    return sorted(items, key=lambda it: -score(it))[:limit]


async def find_series(name: str) -> list[dict]:
    seen: dict[int, dict] = {}
    for q in search_terms(name)[:3]:
        for it in await wp.series(q):
            seen.setdefault(int(it["id"]), it)
        if len(seen) >= 8:
            break
    return rank_items(list(seen.values()), name)


async def find_categories(name: str) -> list[dict]:
    seen: dict[int, dict] = {}
    for q in search_terms(name)[:3]:
        for it in await wp.categories(q):
            seen.setdefault(int(it["id"]), it)
    items = rank_items(list(seen.values()), name) if seen else []
    return items or (await wp.categories(""))[:8]


# ---- embeds ----
_URL = re.compile(r"^https?://\S+$", re.I)
_IFRAME = re.compile(r"<iframe\b", re.I)


def normalize_embed(text: str | None) -> str | None:
    """A pasted <iframe ...> stays as it is, a bare link becomes an iframe, anything else -> None."""
    t = (text or "").strip()
    if not t or len(t) > MAX_CODE:
        return None
    if _URL.match(t):
        return (f'<iframe src="{html.escape(t, quote=True)}" frameborder="0" scrolling="no" '
                'allowfullscreen="true"></iframe>')
    return t if _IFRAME.search(t) else None


def clean_host(text: str | None) -> str:
    return " ".join((text or "").split())[:40]


def embeds_text(flow: Flow) -> str:
    if not flow.embeds:
        return "🎬 <b>Embeds</b>: none"
    lines = [f"🎬 <b>Embeds</b> ({len(flow.embeds)})"]
    for i, e in enumerate(flow.embeds, 1):
        lines.append(f"\n<b>{i}. {esc(e['host'])}</b>\n<pre>{esc(short(e['code'], 220))}</pre>")
    return "\n".join(lines)


# ---- TMDB show + posters ----
def _score(query: str, result: dict) -> float:
    q = normalize(query)
    names = [normalize(result.get("name")), normalize(result.get("original_name"))]
    best = max((similarity(q, n) for n in names if n), default=0.0)
    if "CN" in (result.get("origin_country") or []):
        best += 5
    if 16 in (result.get("genre_ids") or []):
        best += 3
    return best


async def find_show(queries: list[str]) -> tuple[int, str] | None:
    for q in queries:
        if not q:
            continue
        results = await tmdb.search_tv(q)
        if not results:
            continue
        best = max(results[:8], key=lambda r: _score(q, r))
        if _score(q, best) >= 50:
            return int(best["id"]), best.get("name") or q
    return None


async def load_posters(flow: Flow, tv_id: int, name: str) -> None:
    flow.tmdb_id, flow.tmdb_name = tv_id, name
    flow.posters = await tmdb.all_posters(tv_id)
    flow.pos = 0


async def pick_show(flow: Flow, text: str | None = None) -> bool:
    if text:
        tid = parse_tv_id(text)
        if tid:
            tv = await tmdb.tv(tid)
            await load_posters(flow, tid, tv.get("name") or str(tid))
            return bool(flow.posters)
        queries = [text, strip_season(normalize(text))]
    else:
        queries = search_terms(flow.name)[:2] + [flow.series_title]
    hit = await find_show(queries)
    if not hit:
        flow.posters = []
        return False
    await load_posters(flow, *hit)
    return bool(flow.posters)


def image_card(flow: Flow) -> tuple[str, str]:
    n = len(flow.posters)
    flow.pos %= n
    p = flow.posters[flow.pos]
    lang = (p.get("iso_639_1") or "no text").upper()
    caption = (f"🖼 <b>Featured image</b> · {flow.pos + 1}/{n}\n"
               f"<b>{esc(flow.tmdb_name)}</b>\n"
               f"{p.get('width') or '?'}×{p.get('height') or '?'} · language: {esc(lang)}")
    return caption, image_url(p.get("file_path"), "w500")


# =====================================================================
# KEYBOARDS + TEXTS
# =====================================================================
def image_rows(flow: Flow) -> list:
    n = len(flow.posters)
    return [
        [("⬅️ Back", "cb", f"wp:img:{(flow.pos - 1) % n}"), ("✅ Choose", "cb", "wp:img:ok"),
         ("Next ➡️", "cb", f"wp:img:{(flow.pos + 1) % n}")],
        [("🔄 Other show", "cb", "wp:img:other"), ("⏭ Skip image", "cb", "wp:img:skip")],
        [("✖️ Cancel", "cb", "wp:x")],
    ]


def series_rows(flow: Flow) -> list:
    rows = [[(short(it["title"], 48), "cb", f"wp:ser:{i}")] for i, it in enumerate(flow.series)]
    rows.append([("🔎 Search again", "cb", "wp:sq"), ("✖️ Cancel", "cb", "wp:x")])
    return rows


def cat_rows(flow: Flow) -> list:
    rows = [[(short(it["title"], 48), "cb", f"wp:cat:{i}")] for i, it in enumerate(flow.cats)]
    rows.append([("🚫 No category", "cb", "wp:cat:n")])
    rows.append([("🔎 Search again", "cb", "wp:cq"), ("✖️ Cancel", "cb", "wp:x")])
    return rows


EMB_ASK_ROWS = [[("➕ Add", "cb", "wp:emb:add"), ("⏭ Skip", "cb", "wp:emb:skip")], [("✖️ Cancel", "cb", "wp:x")]]


def emb_next_rows(flow: Flow) -> list:
    rows = [[("➕ Add more", "cb", "wp:emb:add"), ("➡️ Next", "cb", "wp:emb:next")]]
    if flow.embeds:
        rows.append([("↩️ Remove last", "cb", "wp:emb:undo"), ("✖️ Cancel", "cb", "wp:x")])
    return rows


NAME_PROMPT = ("📝 <b>New website post</b>\n"
               "Send the post name, for example <code>Soul Land 2</code>.\n"
               "The title becomes <i>name + Episode N English Subtitles</i>, so write the season in the name "
               "if you want it in the title.\n\n/cancel to stop.")

NOT_ENABLED = ("⚠️ Website posting is off. Set <code>WP_BOT_TOKEN</code> (the secret inside "
               "<code>xiaobhai-episode-api.php</code>) in your environment variables and redeploy.")


def post_title(flow: Flow) -> str:
    return episode_title(flow.name, 0, flow.episode)


async def create_post(flow: Flow) -> dict:
    image = image_url(flow.image_path, WP_IMAGE_SIZE) if flow.image_path else ""
    return await wp.create_episode(
        title=post_title(flow), episode=flow.episode, series_id=flow.series_id,
        category_ids=[flow.cat_id] if flow.cat_id else [],
        embeds=flow.embeds, featured_image_url=image, image_title=flow.name)


def result_text(flow: Flow, res: dict) -> tuple[str, list | None]:
    if res.get("exists"):
        head = "ℹ️ <b>This episode is already on the website</b> (nothing was changed)"
    elif res.get("status") == "draft":
        head = "📝 <b>Website draft created</b>"
    else:
        head = "✅ <b>Website post published</b>"
    lines = [head, esc(post_title(flow)),
             f"📚 {esc(flow.series_title)} · 🏷 {esc(flow.cat_title) if flow.cat_id else 'no category'}"]
    if not res.get("exists"):
        if flow.embeds:
            saved, where = res.get("embeds_saved"), res.get("embeds_where")
            if saved is None:
                lines.append("⚠️ Embeds were <b>not saved</b>: the website plugin is old. Upload the new "
                             "<code>xiaobhai-episode-api.php</code> and run /wpcheck.")
            elif where == "content":
                lines.append(f"🎬 {saved} embed(s) saved <b>inside the post text</b> (the theme's embed field "
                             "is not known yet, run /wpcheck).")
            else:
                lines.append(f"🎬 {saved} embed(s) saved")
        if flow.image_path:
            if res.get("api", 1) < 2:
                lines.append("⚠️ Featured image was <b>not set</b>: the website plugin is old.")
            elif res.get("image") == "ok":
                lines.append("🖼 Featured image set")
            else:
                lines.append(f"⚠️ Featured image failed: <code>{esc(short(res.get('image_error') or 'unknown', 200))}</code>")
        for note in res.get("notes") or []:
            lines.append(f"ℹ️ {esc(short(note, 200))}")
    rows = []
    if res.get("edit_url"):
        rows.append([("✏️ Edit post", "url", res["edit_url"])])
    if res.get("link") and res.get("status") == "publish":
        rows.append([("🔗 Open post", "url", res["link"])])
    return "\n".join(lines), rows or None


# =====================================================================
# FLOW STEPS (one function per step)
# =====================================================================
async def _ask_series(msg, flow: Flow, query: str | None = None) -> None:
    q = query or flow.name
    note = await msg.reply_text("🔎 Looking for the series on the website…")
    try:
        items = await find_series(q)
    except WPError as exc:
        flow.step = "series_q"
        await _edit(note, f"⚠️ Website problem: <code>{esc(str(exc)[:300])}</code>\n"
                          "Send a name to try again, or /cancel. Check the connection with /wpcheck.")
        return
    if not items:
        flow.step = "series_q"
        await _edit(note, f"No series on the website matches <b>{esc(q)}</b>.\n"
                          "Send the name as written on the site, or /cancel.")
        return
    flow.series, flow.step = items, "series"
    await _edit(note, f"📚 Which <b>series</b> on the website is <b>{esc(flow.name)}</b>?", series_rows(flow))


async def _ask_category(msg, flow: Flow, query: str | None = None) -> None:
    note = await msg.reply_text("🔎 Looking for categories…")
    try:
        items = await find_categories(query or flow.name)
    except WPError as exc:
        flow.step = "cat_q"
        await _edit(note, f"⚠️ Website problem: <code>{esc(str(exc)[:300])}</code>\n"
                          "Send a category name to try again, or /cancel.")
        return
    flow.cats, flow.step = items, "cat"
    await _edit(note, "🏷 Which <b>category</b> should the post get?", cat_rows(flow))


async def _ask_embeds(msg, flow: Flow) -> None:
    flow.step = "emb_ask"
    await _reply(msg, "🎬 Add a <b>video embed</b>?", EMB_ASK_ROWS)


async def _ask_image(msg, flow: Flow) -> None:
    if not tmdb.enabled:
        await _finish(msg, flow)
        return
    flow.step = "img_wait"
    note = await msg.reply_text("🖼 Looking for posters on TMDB…")
    try:
        found = await pick_show(flow)
    except TMDBError as exc:
        found = False
        log.warning("TMDB poster search failed: %s", exc)
    await note.delete()
    await _show_image(msg, flow, found)


async def _show_image(msg, flow: Flow, found: bool) -> None:
    if not found:
        flow.step = "img_none"
        await _reply(msg, f"🖼 No TMDB posters found for <b>{esc(flow.name)}</b>.",
                     [[("🔄 Other show", "cb", "wp:img:other"), ("⏭ Skip image", "cb", "wp:img:skip")],
                      [("✖️ Cancel", "cb", "wp:x")]])
        return
    flow.step = "img"
    caption, photo = image_card(flow)
    try:
        await msg.reply_photo(photo, caption=caption, parse_mode=HTML, reply_markup=markup(image_rows(flow)))
    except Exception as exc:
        log.warning("poster could not be sent: %s", exc)
        await _reply(msg, caption + "\n<i>(picture could not be loaded, tap Next)</i>", image_rows(flow))


async def _finish(msg, flow: Flow) -> None:
    flow.step = "creating"
    note = await msg.reply_text("⏳ Creating the post…")
    try:
        res = await create_post(flow)
    except Exception as exc:
        flow.step = "final"
        await _edit(note, "⚠️ <b>The post was not created</b>\n" + f"<code>{esc(str(exc)[:400])}</code>",
                    [[("🔁 Retry", "cb", "wp:retry"), ("✖️ Cancel", "cb", "wp:x")]])
        log.warning("Website post failed: %s", exc)
        return
    flow_drop(flow.user_id)
    text, rows = result_text(flow, res)
    await _edit(note, text, rows)


# =====================================================================
# HANDLERS
# =====================================================================
async def wp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not wp.enabled:
        return await _reply(update.message, NOT_ENABLED)
    name = " ".join(context.args or []).strip()
    flow = flow_start(update.effective_user.id, name)
    if name:
        await _ask_series(update.message, flow)
    else:
        await _reply(update.message, NAME_PROMPT)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    done = flow_drop(update.effective_user.id)
    await update.message.reply_text("✖️ Cancelled." if done else "Nothing to cancel.")


class _InFlow(filters.MessageFilter):
    """True when the owner is in the middle of /wp and the next plain text belongs to it."""
    def filter(self, message) -> bool:
        user = message.from_user
        flow = flow_get(user.id) if user else None
        return bool(flow and flow.step in TEXT_STEPS)


async def flow_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    flow = flow_get(update.effective_user.id)
    if not flow:
        return
    flow.touch()
    text = message.text.strip()
    step = flow.step

    if step == "name":
        flow.name = text[:120]
        await _ask_series(message, flow)
    elif step == "series_q":
        await _ask_series(message, flow, text)
    elif step == "cat_q":
        await _ask_category(message, flow, text)
    elif step == "episode":
        m = re.search(r"\d+", text)
        ep = int(m.group()) if m else 0
        if not 1 <= ep <= 9999:
            return await _reply(message, "Send the episode <b>number</b>, for example <code>14</code>.")
        flow.episode = ep
        await _ask_embeds(message, flow)
    elif step == "emb_host":
        host = clean_host(text)
        if not host:
            return await _reply(message, "Send the host name, for example <code>Dailymotion</code>.")
        flow.host, flow.step = host, "emb_code"
        await _reply(message, f"📋 Now send the <b>embed code</b> for <b>{esc(host)}</b> "
                              "(the <code>&lt;iframe …&gt;</code> code, or just the link).")
    elif step == "emb_code":
        code = normalize_embed(text)
        if not code:
            return await _reply(message, "That is not an embed code. Send the <code>&lt;iframe …&gt;</code> code or a link.")
        flow.embeds.append({"host": flow.host, "code": code})
        flow.host, flow.step = "", "emb_next"
        await _reply(message, embeds_text(flow), emb_next_rows(flow))
    elif step == "tmdb_q":
        note = await message.reply_text("🔎 Searching TMDB…")
        try:
            found = await pick_show(flow, text)
        except TMDBError as exc:
            return await _edit(note, f"⚠️ TMDB: <code>{esc(str(exc)[:200])}</code>\nTry another name or link.")
        await note.delete()
        await _show_image(message, flow, found)


async def flow_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    parts = cq.data.split(":")
    action, arg = parts[1], (parts[2] if len(parts) > 2 else "")
    msg = cq.message
    uid = cq.from_user.id

    if action == "x":
        flow_drop(uid)
        await _edit(msg, "✖️ Cancelled.")
        return await cq.answer()
    flow = flow_get(uid)
    if not flow:
        return await cq.answer("This post is no longer open. Send /wp to start again.", show_alert=True)
    flow.touch()
    step = flow.step

    if action == "ser" and step == "series":
        i = int(arg)
        if not 0 <= i < len(flow.series):
            return await cq.answer()
        flow.series_id, flow.series_title = int(flow.series[i]["id"]), flow.series[i]["title"]
        await _edit(msg, f"📚 Series: <b>{esc(flow.series_title)}</b>")
        await cq.answer()
        await _ask_category(msg, flow)
    elif action == "sq" and step == "series":
        flow.step = "series_q"
        await _edit(msg, "🔎 Send a name to search the website series.")
        await cq.answer()
    elif action == "cat" and step == "cat":
        if arg == "n":
            flow.cat_id, flow.cat_title = 0, ""
        else:
            i = int(arg)
            if not 0 <= i < len(flow.cats):
                return await cq.answer()
            flow.cat_id, flow.cat_title = int(flow.cats[i]["id"]), flow.cats[i]["title"]
        await _edit(msg, f"🏷 Category: <b>{esc(flow.cat_title) if flow.cat_id else 'none'}</b>")
        await cq.answer()
        flow.step = "episode"
        await _reply(msg, "🔢 Send the <b>episode number</b>.")
    elif action == "cq" and step == "cat":
        flow.step = "cat_q"
        await _edit(msg, "🔎 Send a category name to search.")
        await cq.answer()
    elif action == "emb" and step in ("emb_ask", "emb_next"):
        if arg == "add":
            if len(flow.embeds) >= MAX_EMBEDS:
                return await cq.answer(f"Maximum {MAX_EMBEDS} hosts.", show_alert=True)
            flow.step = "emb_host"
            await _edit(msg, embeds_text(flow) if flow.embeds else "🎬 Embed: adding…")
            await cq.answer()
            await _reply(msg, "🏷 Send the <b>host name</b>, for example <code>Dailymotion</code>.")
        elif arg == "undo" and flow.embeds:
            flow.embeds.pop()
            await _edit(msg, embeds_text(flow), emb_next_rows(flow) if flow.embeds else EMB_ASK_ROWS)
            if not flow.embeds:
                flow.step = "emb_ask"
            await cq.answer("Removed")
        else:  # skip / next
            await _edit(msg, embeds_text(flow))
            await cq.answer()
            await _ask_image(msg, flow)
    elif action == "img" and step in ("img", "img_none"):
        if arg == "ok" and step == "img":
            flow.image_path = flow.posters[flow.pos]["file_path"]
            await _edit(msg, f"🖼 Featured image: poster {flow.pos + 1}/{len(flow.posters)} of "
                             f"<b>{esc(flow.tmdb_name)}</b>")
            await cq.answer("Poster chosen")
            await _finish(msg, flow)
        elif arg == "skip":
            flow.image_path = ""
            await _edit(msg, "🖼 No featured image.")
            await cq.answer()
            await _finish(msg, flow)
        elif arg == "other":
            flow.step = "tmdb_q"
            await _edit(msg, "🔄 Send the show name or its TMDB link / id.")
            await cq.answer()
        elif arg.isdigit() and step == "img":
            flow.pos = int(arg)
            caption, photo = image_card(flow)
            try:
                await msg.edit_media(InputMediaPhoto(photo, caption=caption, parse_mode=HTML),
                                     reply_markup=markup(image_rows(flow)))
                await cq.answer()
            except Exception as exc:
                log.debug("poster edit failed: %s", exc)
                await cq.answer("Could not load that poster, tap Next.")
        else:
            await cq.answer()
    elif action == "retry" and step == "final":
        await _edit(msg, "⏳ Trying again…")
        await cq.answer()
        await _finish(msg, flow)
    else:
        await cq.answer("That button is old. Use the latest message, or /cancel.", show_alert=True)


def _version(text: str) -> tuple:
    nums = [int(n) for n in re.findall(r"\d+", text)[:2]]
    return tuple(nums + [0] * (2 - len(nums)))


async def wpcheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not wp.enabled:
        return await _reply(msg, NOT_ENABLED)
    wait = await msg.reply_text("🔎 Checking the website…")
    try:
        info = await wp.ping()
        slug = slug_from_url(WP_REFERENCE_URL)
        ep = episode_from_slug(slug)
        if not slug or not ep:
            return await _edit(wait, "⚠️ <code>WP_REFERENCE_URL</code> must be an existing episode page, e.g. "
                                     "<code>https://site/jade-dynasty-season-4-episode-14-english-subtitles/</code>")
        cal = await wp.calibrate(slug, ep)
    except WPError as exc:
        return await _edit(
            wait,
            f"❌ <b>Could not talk to the website</b>\n<code>{esc(str(exc)[:500])}</code>\n\n"
            "• <i>Invalid token</i>: WP_BOT_TOKEN ≠ the secret in the plugin file\n"
            "• <i>firewall page / HTTP 403</i>: allow the bot's IP (or /wp-json/xiaobhai/) in Hostinger / Wordfence / Cloudflare\n"
            "• <i>no route</i>: the plugin file is not in <code>wp-content/mu-plugins/</code>")
    if not cal.get("ok"):
        dump = "\n".join(f"{k} = {v}" for k, v in (cal.get("meta") or {}).items())[:2500]
        return await _edit(
            wait,
            "⚠️ Connected, but I could not recognise these field(s) of the reference post: "
            f"<b>{esc(', '.join(cal.get('missing') or []))}</b>\n"
            f"Send this to your developer/Claude:\n<pre>{esc(dump)}</pre>")
    m = cal["map"]
    plugin = str(info.get("plugin") or "1.0.0")
    new_plugin = _version(plugin) >= (1, 1)
    if m.get("embed_key"):
        embed_line = f"• Embeds → <code>{esc(m['embed_key'])}</code> ({esc(m.get('embed_shape'))})"
    else:
        embed_line = ("• Embeds → <b>not found</b> in the reference post, so they will be put inside the post text. "
                      "Set <code>WP_REFERENCE_URL</code> to an episode that already has an embed and run /wpcheck again.")
    await _edit(
        wait,
        "✅ <b>Website connected</b>\n"
        f"WordPress {esc(info.get('wp'))} · PHP {esc(info.get('php'))} · theme {esc(info.get('theme'))} · "
        f"plugin {esc(plugin)}\n\n"
        "Learned from the reference episode:\n"
        f"• Episode number → <code>{esc(m['episode_key'])}</code>\n"
        f"• Series → <code>{esc(m['series_key'])}</code> (type <code>{esc(m['series_type'])}</code>)\n"
        f"• Subbed? → <code>{esc(m['sub_key'])}</code> = {esc(m['sub_value'])}\n"
        f"{embed_line}\n\n"
        f"New posts are created as: <b>{WP_POST_STATUS}</b>"
        + ("" if new_plugin else "\n\n⚠️ The plugin on the website is <b>old</b>: upload the new "
           "<code>xiaobhai-episode-api.php</code> (embeds and featured image need it)."))


# =====================================================================
# INSTALL INTO NOONA
# =====================================================================
def setup(app, commands: list) -> None:
    """Call once in main() before run_polling. `commands` is Noona's COMMANDS list (menu + /help)."""
    owner = filters.User(user_id=OWNER_ID)
    commands += [
        ("wp", "Create a website post: /wp [name] 📝"),
        ("wpcheck", "Test the website connection 🔌"),
        ("cancel", "Stop the current /wp flow ✖️"),
    ]
    app.add_handler(CommandHandler("wp", wp_cmd, filters=owner))
    app.add_handler(CommandHandler("wpcheck", wpcheck_cmd, filters=owner))
    app.add_handler(CommandHandler("cancel", cancel_cmd, filters=owner))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND & _InFlow(), flow_text))
    app.add_handler(CallbackQueryHandler(flow_button, pattern=r"^wp:"))


async def close() -> None:
    await wp.close()
    await tmdb.close()
