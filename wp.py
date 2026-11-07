"""
NOONA add-on: website posting (WordPress). Owner-only, one self-contained file.

Commands : /wp [name]  - create an episode post step by step
           /edit [x]   - edit an OLD post (title, episode, embeds, poster, category, publish/draft)
           /wpcheck    - test the connection to the website plugin
           /cancel     - stop the current /wp or /edit flow
Flow     : name -> series -> category -> episode -> embeds -> featured image (TMDB) -> post

Install  : see the 3-line change in bot.py (import wp / wp.setup(app, COMMANDS) / await wp.close()).
Env vars : WP_BOT_TOKEN (required)  WP_URL  WP_POST_STATUS  WP_REFERENCE_URL
           TMDB_API_KEY  TMDB_LANGUAGE  WP_IMAGE_SIZE  IMPERSONATE
Server   : wordpress/xiaobhai-episode-api.php (v1.2+) must sit in wp-content/mu-plugins/ on the website.
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

from ui import DIV, cancelled, done, head, oops, script, soft, wait

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

    # ---- editing old posts (plugin 1.2+) ----
    async def posts(self, *, search: str = "", slug: str = "", episode: int = 0, series_id: int = 0) -> list[dict]:
        params = {k: v for k, v in (("search", search), ("slug", slug),
                                    ("episode", episode or ""), ("series_id", series_id or "")) if v}
        return (await self._call("GET", "/posts", params)).get("items") or []

    async def get_post(self, post_id: int) -> dict:
        return await self._call("GET", f"/post/{int(post_id)}")

    async def update_post(self, post_id: int, **fields) -> dict:
        """Send only what changes: title, status, episode, series_id, category_ids, embeds (full list),
        featured_image_url, image_title."""
        body = {k: v for k, v in fields.items() if v is not None}
        return await self._call("POST", f"/post/{int(post_id)}", body=body)


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
        self._posters: dict[int, tuple[float, list]] = {}

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
        hit = self._posters.get(int(tv_id))
        if hit and time.monotonic() - hit[0] < 3600:  # cached for 1 hour
            return list(hit[1])
        data = await self._get(f"/tv/{int(tv_id)}/images", language=None)
        posters = [p for p in (data.get("posters") or []) if p.get("file_path")]
        posters.sort(key=lambda p: (-(p.get("vote_average") or 0), -(p.get("vote_count") or 0)))
        self._posters[int(tv_id)] = (time.monotonic(), posters)
        return list(posters)


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
_edits: dict[int, "Edit"] = {}   # open /edit sessions (see EDIT AN OLD POST)
_last: dict[int, dict] = {}      # last created post per owner, powers the "Next episode" button
TEXT_STEPS = {"name", "series_q", "cat_q", "episode", "emb_host", "emb_code", "tmdb_q"}


def flow_start(user_id: int, name: str = "") -> Flow:
    flow = Flow(user_id=user_id, name=(name or "").strip())
    _edits.pop(user_id, None)  # one open flow at a time
    _flows[user_id] = flow
    return flow


def flow_get(user_id: int) -> Flow | None:
    flow = _flows.get(user_id)
    if flow and time.monotonic() - flow.ts > TTL:
        _flows.pop(user_id, None)
        return None
    return flow


def flow_drop(user_id: int) -> bool:
    a = _flows.pop(user_id, None) is not None
    b = _edits.pop(user_id, None) is not None
    return a or b


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


async def _gather_items(calls) -> dict[int, dict]:
    """Run several website searches at once; fail only if ALL of them fail."""
    results = await asyncio.gather(*calls, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    if errors and len(errors) == len(results):
        raise errors[0]
    seen: dict[int, dict] = {}
    for r in results:
        if not isinstance(r, Exception):
            for it in r:
                seen.setdefault(int(it["id"]), it)
    return seen


async def find_series(name: str) -> list[dict]:
    seen = await _gather_items([wp.series(q) for q in search_terms(name)[:3]])
    return rank_items(list(seen.values()), name)


async def find_categories(name: str) -> list[dict]:
    seen = await _gather_items([wp.categories(q) for q in search_terms(name)[:3]]) if name else {}
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


def embeds_block(embeds: list) -> str:
    if not embeds:
        return f"🎬 <b>{soft('Embeds')}</b>: none"
    lines = [f"🎬 <b>{soft('Embeds')}</b> ({len(embeds)})"]
    for i, e in enumerate(embeds, 1):
        lines.append(f"\n<b>{i}. {esc(e['host'])}</b>\n<pre>{esc(short(e['code'], 220))}</pre>")
    return "\n".join(lines)


def embeds_text(flow: Flow) -> str:
    return embeds_block(flow.embeds)


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


NAME_PROMPT = (f"{head('📝', 'New Website Post')}\n{DIV}\n"
               "Tell me the post name, love, for example <code>Soul Land 2</code> 💕\n"
               "The title becomes <i>name + Episode N English Subtitles</i>, so put the season in the name "
               "if you want it in the title.\n\n/cancel to stop 💋")

EDIT_PROMPT = (f"{head('✏️', 'Edit A Post')}\n{DIV}\n"
               "Send me the <b>post link</b>, the <b>post ID</b>, or part of its name, "
               "for example <code>Soul Land 2 ep 14</code> 💕\n\nOr tap <b>Latest posts</b> 👇")

NOT_ENABLED = ("⚠️ Website posting is off, my love. Set <code>WP_BOT_TOKEN</code> (the secret inside "
               "<code>xiaobhai-episode-api.php</code>) in your environment variables and redeploy 💋")


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
        head_txt = (f"ℹ️ <b>{script('Already There')}</b>\n"
                    "This episode is already on the website, love. I changed nothing 💕")
    elif res.get("status") == "draft":
        head_txt = f"📝 <b>{script('Draft Saved')}</b>\n{done()}"
    else:
        head_txt = f"✅ <b>{script('Published')}</b>\n{done()}"
    lines = [head_txt, DIV, f"<b>{esc(post_title(flow))}</b>",
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
    action_row = []
    if res.get("id"):
        action_row.append(("✏️ Edit this post", "cb", f"ed:open:{res['id']}"))
    action_row.append(("⏭ Next episode", "cb", "wp:next"))
    rows.append(action_row)
    links = []
    if res.get("link") and res.get("status") == "publish":
        links.append(("🔗 Open post", "url", res["link"]))
    if res.get("edit_url"):
        links.append(("🛠 WP editor", "url", res["edit_url"]))
    if links:
        rows.append(links)
    return "\n".join(lines), rows


# =====================================================================
# FLOW STEPS (one function per step)
# =====================================================================
async def _ask_series(msg, flow: Flow, query: str | None = None) -> None:
    q = query or flow.name
    note = await msg.reply_text("🔎 Searching the website for your series, love…")
    try:
        items = await find_series(q)
    except WPError as exc:
        flow.step = "series_q"
        await _edit(note, f"💔 Website problem: <code>{esc(str(exc)[:300])}</code>\n"
                          "Send a name to try again, or /cancel. Check the connection with /wpcheck 💋")
        return
    if not items:
        flow.step = "series_q"
        await _edit(note, f"I couldn't find a series called <b>{esc(q)}</b>, baby 🥺\n"
                          "Send the name as written on the site, or /cancel.")
        return
    flow.series, flow.step = items, "series"
    await _edit(note, f"📚 Which <b>{soft('series')}</b> is <b>{esc(flow.name)}</b>, darling?", series_rows(flow))


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
    await _edit(note, f"🏷 Which <b>{soft('category')}</b> should it go in, love?", cat_rows(flow))


async def _ask_embeds(msg, flow: Flow) -> None:
    flow.step = "emb_ask"
    await _reply(msg, f"🎬 Shall we add a <b>{soft('video embed')}</b>, my love?", EMB_ASK_ROWS)


async def _ask_image(msg, flow: Flow) -> None:
    if flow.image_path:  # "Next episode": same show, same poster
        await msg.reply_text("🖼 Using the same poster as last time 💞")
        await _finish(msg, flow)
        return
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
    note = await msg.reply_text(f"⏳ {wait()}")
    try:
        res = await create_post(flow)
    except Exception as exc:
        flow.step = "final"
        await _edit(note, f"💔 <b>{script('Not Created')}</b>\n{oops()}\n" + f"<code>{esc(str(exc)[:400])}</code>",
                    [[("🔁 Retry", "cb", "wp:retry"), ("✖️ Cancel", "cb", "wp:x")]])
        log.warning("Website post failed: %s", exc)
        return
    flow_drop(flow.user_id)
    if not res.get("exists"):
        _last[flow.user_id] = {
            "name": flow.name, "series_id": flow.series_id, "series_title": flow.series_title,
            "cat_id": flow.cat_id, "cat_title": flow.cat_title, "episode": flow.episode,
            "image_path": flow.image_path, "tmdb_id": flow.tmdb_id, "tmdb_name": flow.tmdb_name,
        }
    text, rows = result_text(flow, res)
    await _edit(note, text, rows)


# =====================================================================
# HANDLERS
# =====================================================================
async def start_new(msg, uid: int, name: str = "") -> None:
    """Start the /wp flow (used by the command and by the menu button)."""
    if not wp.enabled:
        return await _reply(msg, NOT_ENABLED)
    flow = flow_start(uid, name)
    if name:
        await _ask_series(msg, flow)
    else:
        await _reply(msg, NAME_PROMPT)


async def wp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_new(update.message, update.effective_user.id, " ".join(context.args or []).strip())


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    was_open = flow_drop(update.effective_user.id)
    await update.message.reply_text(f"✖️ {cancelled()}" if was_open else "There's nothing to cancel, love 💕")


class _InFlow(filters.MessageFilter):
    """True when the owner is in the middle of /wp and the next plain text belongs to it."""
    def filter(self, message) -> bool:
        user = message.from_user
        if not user:
            return False
        flow, ed = flow_get(user.id), edit_get(user.id)
        return bool((flow and flow.step in TEXT_STEPS) or (ed and ed.step in EDIT_TEXT_STEPS))


async def flow_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    ed = edit_get(update.effective_user.id)
    if ed and ed.step in EDIT_TEXT_STEPS:
        return await edit_text(update, context)
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
            return await _reply(message, "Send the episode <b>number</b>, love, for example <code>14</code> 💕")
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


async def _next_episode(cq, msg, uid: int) -> None:
    """One tap: same series, category and poster, episode + 1, straight to the embeds."""
    last = _last.get(uid)
    if not last:
        return await cq.answer("I lost track of the last post, love. Send /wp 💋", show_alert=True)
    await cq.answer()
    flow = flow_start(uid, last["name"])
    flow.series_id, flow.series_title = last["series_id"], last["series_title"]
    flow.cat_id, flow.cat_title = last["cat_id"], last["cat_title"]
    flow.episode = int(last["episode"]) + 1
    flow.image_path, flow.tmdb_id, flow.tmdb_name = last["image_path"], last["tmdb_id"], last["tmdb_name"]
    flow.step = "emb_ask"
    await _reply(msg, f"⏭ <b>{esc(post_title(flow))}</b>\n📚 {esc(flow.series_title)}\n\n"
                      f"🎬 Shall we add a <b>{soft('video embed')}</b>, my love?", EMB_ASK_ROWS)


async def flow_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    parts = cq.data.split(":")
    action, arg = parts[1], (parts[2] if len(parts) > 2 else "")
    msg = cq.message
    uid = cq.from_user.id

    if action == "x":
        flow_drop(uid)
        await _edit(msg, f"✖️ {cancelled()}")
        return await cq.answer()
    if action == "next":
        return await _next_episode(cq, msg, uid)
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
        await _reply(msg, f"🔢 Which <b>{soft('episode number')}</b>, handsome?")
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
    new_plugin = _version(plugin) >= (1, 2)
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
           "<code>xiaobhai-episode-api.php</code> (embeds, featured image and /edit need version 1.2)."))


# =====================================================================
# EDIT AN OLD POST  (/edit, needs plugin 1.2+)
# =====================================================================
@dataclass
class Edit:
    user_id: int
    step: str = "find"          # find | pick | card | title | episode | emb_host | emb_code | cat_q | tmdb_q ...
    results: list = field(default_factory=list)
    post: dict = field(default_factory=dict)   # last copy of the post, as the website reports it
    host: str = ""
    idx: int = -1               # embed being replaced (-1 = adding a new one)
    cats: list = field(default_factory=list)
    fl: Flow | None = None      # poster picker state (reuses the /wp poster helpers)
    ts: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.ts = time.monotonic()


EDIT_TEXT_STEPS = {"find", "pick", "title", "episode", "emb_host", "emb_code", "cat_q", "tmdb_q"}
_EP = re.compile(r"(?:\bep(?:isode)?\.?\s*|\be)(\d{1,4})\b", re.I)


def edit_start(user_id: int) -> Edit:
    _flows.pop(user_id, None)  # one open flow at a time
    ed = Edit(user_id=user_id)
    _edits[user_id] = ed
    return ed


def edit_get(user_id: int) -> Edit | None:
    ed = _edits.get(user_id)
    if ed and time.monotonic() - ed.ts > TTL:
        _edits.pop(user_id, None)
        return None
    return ed


async def _send(msg, text: str, rows=None):
    """New message in the same chat (works even if `msg` was deleted)."""
    return await msg.chat.send_message(text, parse_mode=HTML, reply_markup=markup(rows),
                                       link_preview_options=NO_PREVIEW)


def _hint(text: str) -> str:
    low = text.lower()
    if "no route" in low or "rest_no_route" in low:
        return "The website plugin is old. Upload the new xiaobhai-episode-api.php (v1.2) and send /wpcheck."
    return short(text, 300)


async def find_posts(query: str) -> list[dict]:
    """Post link, post ID, 'name ep 14', or nothing (= latest posts)."""
    q = (query or "").strip()
    if not q or q.lower() == "latest":
        return await wp.posts()
    if q.isdigit():
        try:
            return [await wp.get_post(int(q))]
        except WPError as exc:
            if "not found" in str(exc).lower():
                return []
            raise
    if q.lower().startswith("http"):
        slug = slug_from_url(q)
        return await wp.posts(slug=slug) if slug else []
    m = _EP.search(q)
    ep = int(m.group(1)) if m else 0
    name = _EP.sub(" ", q) if m else q
    name = " ".join(re.sub(r"\bepisode\b", " ", name, flags=re.I).split())
    items = await wp.posts(search=name, episode=ep) if (name or ep) else []
    if not items and ep and name:
        items = await wp.posts(search=name)
    return items


def edit_card(post: dict, note: str = "") -> str:
    live = post.get("status") == "publish"
    cats = ", ".join(c["title"] for c in post.get("categories") or []) or "none"
    lines = [head("✏️", "Edit Post")]
    if note:
        lines.append(note)
    lines += [
        DIV,
        f"<b>{esc(post.get('title'))}</b>",
        f"📚 {esc(post.get('series_title') or '?')} · 🔢 {soft('Episode')} {post.get('episode') or '?'}",
        f"🏷 {esc(short(cats, 60))}",
        f"{'🌐 Live' if live else '📝 Draft'} · 🖼 {'poster set' if post.get('has_image') else 'no poster'}"
        f" · 🎬 {len(post.get('embeds') or [])} embed(s)",
        "\nWhat shall I change for you, love? 💋",
    ]
    return "\n".join(lines)


def edit_rows(post: dict) -> list:
    live = post.get("status") == "publish"
    rows = [
        [("✏️ Title", "cb", "ed:title"), ("🔢 Episode", "cb", "ed:ep")],
        [("🎬 Embeds", "cb", "ed:emb"), ("🖼 Poster", "cb", "ed:img")],
        [("🏷 Category", "cb", "ed:cat"), ("📝 Make draft" if live else "🌐 Publish", "cb", "ed:st")],
    ]
    links = []
    if live and post.get("link"):
        links.append(("🔗 Open", "url", post["link"]))
    if post.get("edit_url"):
        links.append(("🛠 WP editor", "url", post["edit_url"]))
    if links:
        rows.append(links)
    rows.append([("🔎 Other post", "cb", "ed:find"), ("✖️ Close", "cb", "ed:x")])
    return rows


BACK = [[("↩️ Back", "cb", "ed:back")]]


async def _card(msg, ed: Edit, note: str = "", in_place: bool = False) -> None:
    ed.step = "card"
    text, rows = edit_card(ed.post, note), edit_rows(ed.post)
    if in_place and not msg.photo:
        return await _edit(msg, text, rows)
    if msg.photo:
        try:
            await msg.delete()
        except Exception:
            pass
    await _send(msg, text, rows)


async def _prompt(msg, ed: Edit, step: str, text: str, rows=None) -> None:
    """Replace the current screen with a question (a photo message is swapped for a text one)."""
    ed.step = step
    if msg.photo:
        try:
            await msg.delete()
        except Exception:
            pass
        await _send(msg, text, rows)
    else:
        await _edit(msg, text, rows)


async def _open_post(msg, ed: Edit, post_id: int) -> None:
    try:
        ed.post = await wp.get_post(post_id)
    except WPError as exc:
        return await _send(msg, f"💔 {oops()}\n<code>{esc(_hint(str(exc)))}</code>", None)
    await _card(msg, ed)


async def _apply(msg, ed: Edit, what: str, in_place: bool = False, **fields) -> None:
    """Send one change to the website, reload the post, show the card again."""
    try:
        res = await wp.update_post(ed.post["id"], **fields)
        ed.post = await wp.get_post(ed.post["id"])
    except WPError as exc:
        return await _send(msg, f"💔 {oops()}\n<code>{esc(_hint(str(exc)))}</code>", BACK)
    note = f"✅ {what} updated. {done()}"
    if res.get("image") == "failed":
        note += f"\n⚠️ Poster failed: <code>{esc(short(res.get('image_error') or 'unknown', 160))}</code>"
    for n in res.get("notes") or []:
        note += f"\nℹ️ {esc(short(n, 160))}"
    await _card(msg, ed, note, in_place)


async def _edit_search(msg, ed: Edit, query: str) -> None:
    note = await _send(msg, "🔎 Searching the website for you, love…")
    try:
        items = await find_posts(query)
    except WPError as exc:
        ed.step = "find"
        return await _edit(note, f"💔 Website problem: <code>{esc(_hint(str(exc)))}</code>\n"
                                 "Send another name, link or ID, or /cancel.")
    if not items:
        ed.step = "find"
        return await _edit(note, "I couldn't find that post, baby 🥺\nSend a link, an ID or part of its name, or /cancel.")
    if len(items) == 1:
        await note.delete()
        return await _open_post(msg, ed, int(items[0]["id"]))
    ed.results, ed.step = items, "pick"
    rows = [[(("🌐 " if it.get("status") == "publish" else "📝 ") + short(it["title"], 44), "cb", f"ed:pick:{i}")]
            for i, it in enumerate(items)]
    rows.append([("✖️ Close", "cb", "ed:x")])
    await _edit(note, "🔎 Which one, darling? (or send another search)", rows)


async def start_edit(msg, uid: int, query: str = "") -> None:
    """Start /edit (used by the command and by the menu button)."""
    if not wp.enabled:
        return await _reply(msg, NOT_ENABLED)
    ed = edit_start(uid)
    if query:
        return await _edit_search(msg, ed, query)
    await _send(msg, EDIT_PROMPT, [[("🕒 Latest posts", "cb", "ed:latest")], [("✖️ Close", "cb", "ed:x")]])


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_edit(update.message, update.effective_user.id, " ".join(context.args or []).strip())


# ---- embeds screen ----
def _emb_rows(embeds: list) -> list:
    rows = []
    for i, e in enumerate(embeds):
        rows.append([(f"♻️ Replace {i + 1}: {short(e['host'], 14)}", "cb", f"ed:ere:{i}"),
                     (f"🗑 {i + 1}", "cb", f"ed:edel:{i}")])
    rows.append([("➕ Add embed", "cb", "ed:eadd")])
    if embeds:
        rows.append([("🧹 Remove all", "cb", "ed:eclr")])
    rows.append([("↩️ Back", "cb", "ed:back")])
    return rows


async def _emb_screen(msg, ed: Edit) -> None:
    embeds = ed.post.get("embeds") or []
    await _prompt(msg, ed, "card", f"{head('🎬', 'Embeds')}\n{DIV}\n{embeds_block(embeds)}", _emb_rows(embeds))


# ---- poster screen (TMDB) ----
def _poster_rows(fl: Flow) -> list:
    n = len(fl.posters)
    return [
        [("⬅️ Back", "cb", f"ed:im:{(fl.pos - 1) % n}"), ("✅ Choose", "cb", "ed:iok"),
         ("Next ➡️", "cb", f"ed:im:{(fl.pos + 1) % n}")],
        [("🔄 Other show", "cb", "ed:io"), ("↩️ Back", "cb", "ed:back")],
    ]


async def _poster_start(msg, ed: Edit) -> None:
    if not tmdb.enabled:
        return await _prompt(msg, ed, "card", "🖼 Set <code>TMDB_API_KEY</code> to pick posters, love 💋", BACK)
    ed.fl = Flow(user_id=ed.user_id, name=ed.post.get("series_title") or ed.post.get("title") or "")
    note = await _send(msg, "🔎 Looking for posters, love…")
    try:
        found = await pick_show(ed.fl)
    except TMDBError as exc:
        log.warning("TMDB poster search failed: %s", exc)
        found = False
    await note.delete()
    await _poster_show(msg, ed, found)


async def _poster_show(msg, ed: Edit, found: bool) -> None:
    if not found:
        ed.step = "tmdb_q"
        return await _send(msg, f"🖼 No posters found for <b>{esc(ed.fl.name)}</b> 🥺\n"
                                "Send the show name or its TMDB link / id.", BACK)
    ed.step = "poster"
    caption, photo = image_card(ed.fl)
    try:
        await msg.chat.send_photo(photo, caption=caption, parse_mode=HTML, reply_markup=markup(_poster_rows(ed.fl)))
    except Exception as exc:
        log.warning("poster could not be sent: %s", exc)
        await _send(msg, caption + "\n<i>(picture could not be loaded, tap Next)</i>", _poster_rows(ed.fl))


# ---- text answers ----
async def edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ed = edit_get(update.effective_user.id)
    if not ed:
        return
    ed.touch()
    text, step = msg.text.strip(), ed.step

    if step in ("find", "pick"):
        await _edit_search(msg, ed, text)
    elif step == "title":
        new = " ".join(text.split())[:250]
        if not new:
            return await _reply(msg, "Send me the new title, love 💕")
        await _apply(msg, ed, "Title", title=new)
    elif step == "episode":
        m = re.search(r"\d+", text)
        ep = int(m.group()) if m else 0
        if not 1 <= ep <= 9999:
            return await _reply(msg, "Send the episode <b>number</b>, love, for example <code>14</code> 💕")
        await _apply(msg, ed, "Episode number", episode=ep)
    elif step == "emb_host":
        host = clean_host(text)
        if not host:
            return await _reply(msg, "Send the host name, for example <code>Dailymotion</code> 💕")
        ed.host, ed.step = host, "emb_code"
        await _reply(msg, f"📋 Now the <b>embed code</b> for <b>{esc(host)}</b> "
                          "(the <code>&lt;iframe …&gt;</code> code, or just the link).")
    elif step == "emb_code":
        code = normalize_embed(text)
        if not code:
            return await _reply(msg, "That doesn't look like an embed, baby. Send the <code>&lt;iframe …&gt;</code> code or a link.")
        embeds = [dict(host=e["host"], code=e["code"]) for e in ed.post.get("embeds") or []]
        item = {"host": ed.host, "code": code}
        if 0 <= ed.idx < len(embeds):
            embeds[ed.idx] = item
        else:
            embeds.append(item)
        ed.idx = -1
        await _apply(msg, ed, "Embeds", embeds=embeds)
    elif step == "cat_q":
        try:
            ed.cats = await find_categories(text)
        except WPError as exc:
            return await _reply(msg, f"💔 <code>{esc(_hint(str(exc)))}</code>")
        await _send(msg, "🏷 Pick the new <b>category</b>, love (it replaces the old one):", _cat_rows(ed))
        ed.step = "cat"
    elif step == "tmdb_q":
        note = await msg.reply_text("🔎 Searching TMDB…")
        try:
            found = await pick_show(ed.fl, text)
        except TMDBError as exc:
            return await _edit(note, f"⚠️ TMDB: <code>{esc(str(exc)[:200])}</code>\nTry another name or link.")
        await note.delete()
        await _poster_show(msg, ed, found)


def _cat_rows(ed: Edit) -> list:
    rows = [[(short(it["title"], 48), "cb", f"ed:cs:{i}")] for i, it in enumerate(ed.cats)]
    rows.append([("🔎 Search", "cb", "ed:cq"), ("↩️ Back", "cb", "ed:back")])
    return rows


# ---- buttons ----
async def edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    parts = cq.data.split(":")
    action, arg = parts[1], (parts[2] if len(parts) > 2 else "")
    msg, uid = cq.message, cq.from_user.id

    if action == "x":
        _edits.pop(uid, None)
        await _edit(msg, f"✖️ {cancelled()}")
        return await cq.answer()
    if action == "open":  # from the "post created" card: ed:open:<post id>
        await cq.answer()
        if not wp.enabled:
            return await _reply(msg, NOT_ENABLED)
        return await _open_post(msg, edit_start(uid), int(arg))

    ed = edit_get(uid)
    if not ed:
        return await cq.answer("This editor closed, love. Send /edit to open it again 💋", show_alert=True)
    ed.touch()
    await cq.answer()
    post = ed.post

    if action == "find":
        await _prompt(msg, ed, "find", EDIT_PROMPT, [[("🕒 Latest posts", "cb", "ed:latest")], [("✖️ Close", "cb", "ed:x")]])
    elif action == "latest":
        await _edit_search(msg, ed, "")
    elif action == "pick":
        i = int(arg)
        if 0 <= i < len(ed.results):
            await _open_post(msg, ed, int(ed.results[i]["id"]))
    elif not post:
        await _prompt(msg, ed, "find", EDIT_PROMPT, [[("🕒 Latest posts", "cb", "ed:latest")], [("✖️ Close", "cb", "ed:x")]])
    elif action == "back":
        await _card(msg, ed, in_place=True)
    elif action == "title":
        await _prompt(msg, ed, "title", f"✏️ Send the <b>new title</b>, love.\nNow: <code>{esc(post.get('title'))}</code>", BACK)
    elif action == "ep":
        await _prompt(msg, ed, "episode", f"🔢 Send the <b>new episode number</b>.\nNow: <code>{post.get('episode') or '?'}</code>", BACK)
    elif action == "st":
        new = "draft" if post.get("status") == "publish" else "publish"
        await _apply(msg, ed, "Status", in_place=True, status=new)
    elif action == "emb":
        await _emb_screen(msg, ed)
    elif action == "eadd":
        if len(post.get("embeds") or []) >= MAX_EMBEDS:
            return await _send(msg, f"That's the maximum of {MAX_EMBEDS} embeds, love 💕", BACK)
        ed.idx = -1
        await _prompt(msg, ed, "emb_host", "🏷 Send the <b>host name</b>, for example <code>Dailymotion</code>.", BACK)
    elif action == "ere":
        i = int(arg)
        embeds = post.get("embeds") or []
        if 0 <= i < len(embeds):
            ed.idx, ed.host = i, embeds[i]["host"]
            await _prompt(msg, ed, "emb_code", f"📋 Send the new <b>embed code</b> for <b>{esc(ed.host)}</b> "
                                               "(the <code>&lt;iframe …&gt;</code> code, or just the link).", BACK)
    elif action == "edel":
        i = int(arg)
        embeds = [dict(host=e["host"], code=e["code"]) for e in post.get("embeds") or []]
        if 0 <= i < len(embeds):
            embeds.pop(i)
            await _apply(msg, ed, "Embeds", in_place=True, embeds=embeds)
    elif action == "eclr":
        await _apply(msg, ed, "Embeds", in_place=True, embeds=[])
    elif action == "cat":
        try:
            ed.cats = await find_categories(post.get("series_title") or "")
        except WPError as exc:
            return await _send(msg, f"💔 <code>{esc(_hint(str(exc)))}</code>", BACK)
        await _prompt(msg, ed, "cat", "🏷 Pick the new <b>category</b>, love (it replaces the old one):", _cat_rows(ed))
    elif action == "cs":
        i = int(arg)
        if 0 <= i < len(ed.cats):
            await _apply(msg, ed, "Category", in_place=True, category_ids=[int(ed.cats[i]["id"])])
    elif action == "cq":
        await _prompt(msg, ed, "cat_q", "🔎 Send a category name to search.", BACK)
    elif action == "img":
        await _poster_start(msg, ed)
    elif action == "im" and ed.fl and ed.fl.posters:
        ed.fl.pos = int(arg) % len(ed.fl.posters)
        caption, photo = image_card(ed.fl)
        try:
            await msg.edit_media(InputMediaPhoto(photo, caption=caption, parse_mode=HTML),
                                 reply_markup=markup(_poster_rows(ed.fl)))
        except Exception as exc:
            log.debug("poster edit failed: %s", exc)
    elif action == "iok" and ed.fl and ed.fl.posters:
        path = ed.fl.posters[ed.fl.pos]["file_path"]
        await _apply(msg, ed, "Poster", featured_image_url=image_url(path, WP_IMAGE_SIZE),
                     image_title=ed.fl.tmdb_name or ed.fl.name)
    elif action == "io":
        await _prompt(msg, ed, "tmdb_q", "🔄 Send the show name or its TMDB link / id.", BACK)


# =====================================================================
# INSTALL INTO NOONA
# =====================================================================
def setup(app, commands: list) -> None:
    """Call once in main() before run_polling. `commands` is Noona's COMMANDS list (menu + /help)."""
    owner = filters.User(user_id=OWNER_ID)
    commands += [
        ("wp", "Create a website post: /wp [name] 📝"),
        ("edit", "Edit an old website post: /edit [link, id or name] ✏️"),
        ("wpcheck", "Test the website connection 🔌"),
        ("cancel", "Stop the current /wp or /edit flow ✖️"),
    ]
    app.add_handler(CommandHandler("wp", wp_cmd, filters=owner))
    app.add_handler(CommandHandler("edit", edit_cmd, filters=owner))
    app.add_handler(CommandHandler("wpcheck", wpcheck_cmd, filters=owner))
    app.add_handler(CommandHandler("cancel", cancel_cmd, filters=owner))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND & _InFlow(), flow_text))
    app.add_handler(CallbackQueryHandler(flow_button, pattern=r"^wp:"))
    app.add_handler(CallbackQueryHandler(edit_button, pattern=r"^ed:"))


async def close() -> None:
    await wp.close()
    await tmdb.close()
