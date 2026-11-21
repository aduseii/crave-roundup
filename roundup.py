#!/usr/bin/env python3
"""
Sitcom Crave Roundup.

Collects comedy TV news from trade RSS feeds, Google News searches and YouTube
channels, keeps the comedy stories, merges duplicates and writes plain HTML
pages into docs/ (served by GitHub Pages).

    python roundup.py morning    overnight sweep, anniversaries, upcoming premieres
    python roundup.py evening    what is new since the morning
    python roundup.py breaking   quick check: Telegram alert + breaking page

Optional environment variables (set as GitHub secrets):
    TMDB_API_KEY         upcoming comedy premieres from TMDB
    TELEGRAM_BOT_TOKEN   breaking-news alerts
    TELEGRAM_CHAT_ID     the chat that receives the alerts
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

import feedparser
import requests

ROOT = Path(os.environ.get("ROUNDUP_ROOT") or Path(__file__).resolve().parent)
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
STATE_FILE = DATA_DIR / "state.json"

UTC = dt.timezone.utc
KEEP_DAYS = 7          # how long seen stories are remembered
FRESH_HOURS = 720      # default: stories published more than 30 days ago are never shown (config: freshness.max_age_hours)
BREAKING_ALERT_HOURS = 6   # default: only stories this new trigger a Telegram alert
REPEAT_HOURS = 720     # a story shown in a roundup isn't shown again for 30 days
BREAKING_PAGE_HOURS = 24
SECTION_CAP = 40       # stories shown per section before "More"
MAX_ALERTS = 5         # Telegram alerts per run; the rest go in one summary message

BREAKING_TAGS = ["Death", "Cancel/End", "Renewal", "Exit", "Arrest", "Revival"]

SECTIONS = [
    ("breaking", "Breaking"),
    ("news", "News"),
    ("streaming", "Streaming"),
    ("interviews", "Interviews and podcasts"),
    ("trailers", "Trailers"),
    ("upcoming", "Upcoming"),
]
JUMP_LABELS = {
    "breaking": "Breaking",
    "news": "News",
    "streaming": "Streaming",
    "interviews": "Interviews",
    "trailers": "Trailers",
    "upcoming": "Upcoming",
}
SECTION_TYPE = {
    "breaking": "Breaking",
    "news": "News",
    "streaming": "Streaming",
    "interviews": "Interview",
    "trailers": "Trailer",
    "upcoming": "Upcoming",
}

DEFAULT_HEADER = ""   # nothing added above the stories; Grok handles the writing

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

_SECRETS: list[str] = []


def redact(text: str) -> str:
    """Hide API keys and tokens from anything that gets logged or saved,
    including padded copies and partial pieces of them."""
    for whole in _SECRETS:
        for secret in (whole or "").split():
            if len(secret) < 10:
                continue
            text = text.replace(secret, "***")
            # any 10+ character stretch of the secret (a cut-off or split copy)
            for size in range(len(secret) - 1, 9, -1):
                for start in range(0, len(secret) - size + 1):
                    piece = secret[start:start + size]
                    if piece in text:
                        text = text.replace(piece, "***")
    return text


def log(msg: str) -> None:
    print(redact(msg), flush=True)


def now_utc() -> dt.datetime:
    fixed = os.environ.get("ROUNDUP_NOW")
    if fixed:
        return dt.datetime.fromisoformat(fixed.replace("Z", "+00:00")).astimezone(UTC)
    return dt.datetime.now(UTC)


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def iso(d: dt.datetime | None) -> str | None:
    return d.astimezone(UTC).isoformat(timespec="seconds") if d else None


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def us_date(d: dt.date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def long_date(d: dt.date) -> str:
    return f"{d.strftime('%A')}, {us_date(d)}"


def short_date(d: dt.date) -> str:
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def clock(d: dt.datetime) -> str:
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d}{'am' if d.hour < 12 else 'pm'}"


def ago(d: dt.datetime | None, now: dt.datetime) -> str:
    if not d:
        return ""
    mins = int((now - d).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    if mins < 48 * 60:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


def fold(text: str) -> str:
    """Straighten curly quotes and drop accents so matching is reliable."""
    text = (text.replace("‘", "'").replace("’", "'").replace("‛", "'")
                .replace("′", "'").replace("“", '"').replace("”", '"')
                .replace("‟", '"').replace(" ", " "))
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def clean_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*The post .{0,300}? appeared first on .{0,80}?\.?$", "", s)
    s = re.sub(r"\s*\[(?:…|\.\.\.)\]\s*$", "...", s)
    return s.replace("—", ", ").replace(" , ", ", ").strip()


def trim(s: str, limit: int = 300) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit * 0.5:
        return cut[: end + 1]
    return cut.rsplit(" ", 1)[0] + "..."


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

SESSION = requests.Session()
USER_AGENT = "SitcomCraveRoundup/1.0"


class FetchError(Exception):
    pass


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             timeout: int = 25) -> requests.Response:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    last: Exception | None = None
    for attempt in range(2):
        try:
            r = SESSION.get(url, params=params, headers=h, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(3)
                continue
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}")
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2)
    raise FetchError(type(last).__name__ if last else "request failed")


# --------------------------------------------------------------------------
# Matching and tagging
# --------------------------------------------------------------------------

def load_not_news() -> list[re.Pattern]:
    pats = []
    for line in load_lines(CONFIG_DIR / "not_news.txt") or load_lines(
            Path(__file__).resolve().parent / "config" / "not_news.txt"):
        try:
            pats.append(re.compile(line, re.I))
        except re.error:
            log(f"  not_news.txt: skipped a pattern that isn't valid: {line}")
    return pats


NOT_NEWS: list[re.Pattern] = []


class Watchlist:
    def __init__(self, shows: list[str], people: list[str]):
        self.shows: list[tuple[str, re.Pattern]] = []
        self.search_shows: list[str] = []
        for line in shows:
            quoted = line.endswith("*")
            name = line[:-1].strip() if quoted else line.strip()
            esc = re.escape(fold(name))
            if quoted:
                pat = re.compile(r"['\"]" + esc + r"[!?.]?['\"]")
            else:
                flags = re.I if len(name.split()) >= 2 else 0
                pat = re.compile(r"(?<![\w])" + esc + r"(?![\w])", flags)
            if not quoted or len(name.split()) >= 2:
                # one-word titles like Ghosts* are too vague to search for
                self.search_shows.append(name)
            self.shows.append((name, pat))
        self.people = [
            (n, re.compile(r"(?<![\w])" + re.escape(fold(n)) + r"(?![\w])", re.I))
            for n in people
        ]

    def match(self, text: str) -> tuple[list[str], list[str]]:
        t = fold(text)
        shows = [n for n, p in self.shows if p.search(t)]
        people = [n for n, p in self.people if p.search(t)]
        return shows, people


COMEDY_RE = re.compile(
    r"\b(?:comed(?:y|ies|ic|ian|ians|ienne)|sitcoms?|stand-?up|late[- ]night|snl|"
    r"saturday night live|multi-?cam(?:era)?|laugh track)\b", re.I)
TV_RE = re.compile(
    r"\b(?:series|seasons?|episodes?|shows?|sitcoms?|tv|television|streaming|streamer|"
    r"network|pilot|special|renew\w*|cancel\w*|netflix|hulu|peacock|hbo|max|prime video|"
    r"paramount|disney|apple tv|abc|nbc|cbs|fox|the cw|fx|comedy central|bet)\b", re.I)
SITCOM_RE = re.compile(r"\bsitcoms?\b", re.I)

STREAMERS = (r"(?:netflix|hulu|peacock|hbo max|max|prime video|amazon prime|paramount\+|"
             r"paramount plus|disney\+|disney plus|apple tv\+?|tubi|pluto tv|roku channel|bet\+)")
STREAM_RE = re.compile(
    r"\b(?:arriv\w*|coming to|leav(?:es|ing)|lands? on|now streaming|streaming (?:on|now)|"
    r"heads? to|moving to|moves? to|new on|what'?s new|added to|to stream on|exits?)\b"
    r"[^.]{0,40}?\b" + STREAMERS + r"(?!\w)"
    r"|\b" + STREAMERS + r"(?!\w)[^.]{0,30}?\b(?:this week|this month|this weekend|arrivals|"
    r"departures|new releases)\b", re.I)
STREAM_LEAVING_RE = re.compile(
    r"\b(?:leav(?:es|ing)|exits?)\s+(?:on\s+)?" + STREAMERS + r"(?!\w)", re.I)

TAG_RULES: list[tuple[str, re.Pattern]] = [
    ("Death", re.compile(
        r"\b(?:dies|died|dead at \d+|has died|passes away|passed away|obituary|cause of death|"
        r"dead,? (?:at|aged) \d+)\b", re.I)),
    ("Cancel/End", re.compile(
        r"\b(?:cancel(?:s|ed|led|ing|lation)|axed|axes|scrapped|not (?:be )?renewed|"
        r"to end (?:with|after)|ending after|final season|series finale date)\b", re.I)),
    ("Renewal", re.compile(
        r"\b(?:renew(?:s|ed|al)|picked up for (?:a )?(?:second|third|fourth|fifth|sixth|"
        r"seventh|eighth|ninth|tenth|\d+\w*) season)\b", re.I)),
    ("Exit", re.compile(
        r"\b(?:exits?|exiting|departs?|departing|departure|to leave|leaves|leaving|fired|"
        r"ousted|quits?|steps? down|won'?t return|will not return|not returning)\b", re.I)),
    ("Arrest", re.compile(r"\b(?:arrested|charged with|indicted)\b", re.I)),
    ("Revival", re.compile(
        r"\b(?:revival|reboot|revived?|revives|spinoff|spin-off|sequel series)\b", re.I)),
    ("Casting", re.compile(
        r"\b(?:cast as|casts|joins|to star in|set to star|boards|series regular|"
        r"lands? (?:lead|role))\b", re.I)),
    ("Trailer", re.compile(r"\b(?:trailer|teaser|first look|sneak peek|promo)\b", re.I)),
    ("Upcoming", re.compile(
        r"\b(?:premiere date|sets? (?:premiere|return|debut)|premieres?|premiering|"
        r"debuts?|return date|release date|first look)\b", re.I)),
    ("Interview", re.compile(
        r"\b(?:interview|podcast|opens up|reveals?|recalls?|reflects?|admits?|explains?|"
        r"says|talks?|spills|dishes|addresses|responds?|q&a|tells)\b", re.I)),
]


def tag_title(title: str) -> list[str]:
    t = fold(title)
    tags = []
    streaming = bool(STREAM_RE.search(t))
    exit_text = STREAM_LEAVING_RE.sub(" ", t)
    for name, pat in TAG_RULES:
        if name == "Exit":
            if pat.search(exit_text):
                tags.append(name)
        elif pat.search(t):
            tags.append(name)
    if streaming:
        tags.append("Streaming")
    return tags


def annotate(item: dict, wl: Watchlist) -> dict:
    text = f"{item['title']} {item.get('summary', '')}"
    shows, people = wl.match(text)
    folded = fold(text)
    sitcom = bool(SITCOM_RE.search(folded))
    comedy_tv = bool(COMEDY_RE.search(folded) and TV_RE.search(folded))
    item["shows"] = shows
    item["people"] = people
    item["sitcom"] = sitcom
    item["relevant"] = bool(shows or people or sitcom or comedy_tv or item.get("query_relevant"))
    # "strong": the headline itself names a watched show/person or says sitcom/comedy TV.
    # Only strong stories can be breaking news.
    t_shows, t_people = wl.match(item["title"])
    t = fold(item["title"])
    item["strong"] = bool(t_shows or t_people or SITCOM_RE.search(t)
                          or (COMEDY_RE.search(t) and TV_RE.search(t)))
    item["tags"] = tag_title(item["title"])
    item["rehash"] = item["kind"] == "article" and any(p.search(fold(item["title"])) for p in NOT_NEWS)
    if item["rehash"]:
        item["relevant"] = False
    return item


# --------------------------------------------------------------------------
# Collecting
# --------------------------------------------------------------------------

def entry_time(e) -> dt.datetime | None:
    for k in ("published_parsed", "updated_parsed"):
        v = e.get(k)
        if v:
            try:
                return dt.datetime(*v[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                pass
    return None


def parse_feed(content: bytes):
    feed = feedparser.parse(content)
    if not feed.entries:
        reason = feed.get("bozo_exception")
        raise FetchError(f"no stories in feed{f' ({reason})' if reason else ''}")
    return feed


IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:$|[?#])", re.I)
IMG_SRC_RE = re.compile(r"""<img[^>]+?src=["']([^"']+)["']""", re.I)


def clean_image(url: str | None) -> str:
    """A usable https picture URL, or ''. Tracking pixels and icons are dropped."""
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[7:]
    if not url.startswith("https://") or len(url) > 1000:
        return ""
    low = url.lower()
    if any(b in low for b in ("pixel", "feedburner", "spacer", "1x1", "gravatar", "/emoji/", "favicon", "logo")):
        return ""
    return url


def entry_image(e) -> str:
    """The story's picture from a feed entry: media tags, an image enclosure, or the first <img>."""
    for m in (e.get("media_content") or []):
        if m.get("medium") in (None, "", "image") and not str(m.get("type", "")).startswith(("video", "audio")):
            u = clean_image(m.get("url"))
            if u:
                return u
    for m in (e.get("media_thumbnail") or []):
        u = clean_image(m.get("url"))
        if u:
            return u
    for link in e.get("links", []):
        if link.get("rel") == "enclosure" and (str(link.get("type", "")).startswith("image")
                                               or IMG_EXT_RE.search(link.get("href", ""))):
            u = clean_image(link.get("href"))
            if u:
                return u
    raw = (e.get("summary", "") or "") + "".join(c.get("value", "") for c in e.get("content", []) or [])
    m = IMG_SRC_RE.search(raw)
    return clean_image(m.group(1)) if m else ""


def fetch_rss(name: str, url: str, params: dict | None = None) -> tuple[list[dict], str]:
    feed = parse_feed(http_get(url, params=params).content)
    items = []
    for e in feed.entries:
        title = clean_html(e.get("title", ""))
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        items.append({
            "id": sha(link),
            "title": title,
            "link": link,
            "outlet": name,
            "source_type": "direct",
            "kind": "article",
            "published": iso(entry_time(e)),
            "summary": trim(clean_html(e.get("summary", "") or e.get("description", ""))),
            "image": entry_image(e),
        })
    return items, feed.feed.get("title", "")


def outlet_allowed(outlet: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    o = fold(outlet or "").lower().strip()
    for a in allowed:
        a = fold(a).lower().strip()
        if o == a or o.startswith(a + " ") or o == "www." + a:
            return True
    return False


def fetch_gnews(cfg: dict, query: str, topical: bool = False) -> list[dict]:
    window = cfg.get("window", "1d")
    params = dict(cfg.get("params", {}))
    params["q"] = f"{query} when:{window}" if window else query
    feed = parse_feed(http_get(cfg["base"], params=params).content)
    items = []
    for e in feed.entries:
        title = clean_html(e.get("title", ""))
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        src = e.get("source") or {}
        outlet = src.get("title") if hasattr(src, "get") else None
        if outlet and title.endswith(" - " + outlet):
            title = title[: -(len(outlet) + 3)].strip()
        if not outlet_allowed(outlet or "", cfg.get("allowed_outlets", [])):
            continue
        items.append({
            "id": sha(link),
            "title": title,
            "link": link,
            "outlet": outlet or "Google News",
            "source_type": "gnews",
            "kind": "article",
            "published": iso(entry_time(e)),
            "summary": "",
            "query": query,
            "query_relevant": topical,
        })
    return items


def fetch_youtube(ch: dict) -> tuple[list[dict], str]:
    url = "https://www.youtube.com/feeds/videos.xml"
    feed = parse_feed(http_get(url, params={"channel_id": ch["channel_id"]}).content)
    channel_title = feed.feed.get("title", "")
    items = []
    for e in feed.entries:
        title = clean_html(e.get("title", ""))
        vid = e.get("yt_videoid") or ""
        link = e.get("link") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
        if not title or not link:
            continue
        desc = e.get("media_description") or e.get("summary", "")
        items.append({
            "id": sha(link),
            "title": title,
            "link": link,
            "outlet": channel_title or ch["name"],
            "source_type": "youtube",
            "kind": "video",
            "channel_kind": ch.get("kind", "outlet"),
            "published": iso(entry_time(e)),
            "summary": trim(clean_html(desc), 240),
            "image": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else entry_image(e),
        })
    return items, channel_title


def watch_queries(wl: Watchlist, batch: int) -> list[str]:
    names = [f'"{n}"' for n in wl.search_shows] + [f'"{n}"' for n, _ in wl.people]
    return [" OR ".join(names[i:i + batch]) for i in range(0, len(names), batch)]


def collect(mode: str, cfg: dict, wl: Watchlist) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    health: list[dict] = []

    def record(kind, name, fn):
        try:
            got = fn()
            extra = ""
            if isinstance(got, tuple):
                got, extra = got
            items.extend(got)
            health.append({"type": kind, "name": name, "ok": True, "count": len(got),
                           "resolved": extra})
            log(f"  ok   {kind:8} {name}: {len(got)}")
        except Exception as e:  # one broken source must never stop the run
            msg = redact(f"{type(e).__name__}: {e}")[:200]
            health.append({"type": kind, "name": name, "ok": False, "count": 0, "error": msg})
            log(f"  FAIL {kind:8} {name}: {msg}")

    log("Trade feeds")
    for f in cfg.get("feeds", []):
        record("feed", f["name"], lambda f=f: fetch_rss(f["name"], f["url"]))

    gn = cfg.get("google_news", {})
    pause = float(gn.get("pause_seconds", 1.0))
    # (query, topical): topical searches are comedy-specific, so every result counts
    queries = [(q, True) for q in gn.get("breaking_queries", [])]
    if mode != "breaking":
        queries = ([(q, True) for q in gn.get("queries", [])] + queries
                   + [(q, False) for q in watch_queries(wl, int(gn.get("watchlist_batch", 6)))])
    log(f"Google News ({len(queries)} searches)")
    for q, topical in queries:
        record("search", q, lambda q=q, t=topical: fetch_gnews(gn, q, t))
        time.sleep(pause)

    if mode != "breaking":
        log("YouTube")
        for ch in cfg.get("youtube", []):
            record("youtube", ch["name"], lambda ch=ch: fetch_youtube(ch))
    return items, health


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("state.json was unreadable, starting fresh")
    return {"items": {}, "editions": [], "alerted": {}}


def save_state(state: dict) -> None:
    write_text(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True))


def merge(state: dict, raw: list[dict], wl: Watchlist, now: dt.datetime) -> int:
    added = 0
    store = state["items"]
    for it in raw:
        annotate(it, wl)
        if not it["relevant"] and it["kind"] != "video":
            continue
        old = store.get(it["id"])
        if old:
            it["first_seen"] = old["first_seen"]
            if not it.get("image") and old.get("image"):
                it["image"] = old["image"]
        else:
            it["first_seen"] = iso(now)
            added += 1
        store[it["id"]] = it
    return added


def refilter(state: dict, wl: Watchlist, cfg: dict) -> None:
    """Re-check stored stories with the current watchlist and outlet list,
    so edits to the config apply straight away."""
    allowed = cfg.get("google_news", {}).get("allowed_outlets", [])
    kept = {}
    for k, it in state["items"].items():
        if it.get("source_type") == "gnews" and not outlet_allowed(it.get("outlet", ""), allowed):
            continue
        annotate(it, wl)
        if it["kind"] == "article" and not it["relevant"]:
            continue
        kept[k] = it
    state["items"] = kept


def prune(state: dict, now: dt.datetime) -> None:
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    state["items"] = {k: v for k, v in state["items"].items()
                      if (parse_iso(v.get("first_seen")) or now) >= cutoff}
    state["alerted"] = {k: v for k, v in state.get("alerted", {}).items()
                        if (parse_iso(v) or now) >= cutoff}
    state["editions"] = state.get("editions", [])[-120:]
    state["drafts"] = {k: v for k, v in state.get("drafts", {}).items()
                       if (parse_iso(v.get("at")) or now) >= cutoff}
    state["images"] = {k: v for k, v in state.get("images", {}).items()
                       if (parse_iso(v.get("at")) or now) >= cutoff}


def item_time(it: dict) -> dt.datetime:
    return parse_iso(it.get("published")) or parse_iso(it.get("first_seen")) or dt.datetime.min.replace(tzinfo=UTC)


def is_fresh(it: dict, now: dt.datetime, hours: float | None = None) -> bool:
    """Only stories with a known publish time inside the window count as news."""
    pub = parse_iso(it.get("published"))
    return pub is not None and now - dt.timedelta(hours=hours or FRESH_HOURS) <= pub <= now + dt.timedelta(hours=1)


# --------------------------------------------------------------------------
# Merging duplicate stories
# --------------------------------------------------------------------------

STOP = set("""a an the and or of to in on for with at by from as is are be was were it its this that
these those new star stars says season series show shows tv his her their about after over into
will has have had more than just what who why how when first one two out up""".split())


def stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "al", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", fold(title).lower())
    return {stem(w) for w in words if len(w) > 2 and w not in STOP}


def same_event(it: dict, c: dict) -> bool:
    """Different headlines about the same renewal/death/etc. of the same show or person."""
    tags = set(it.get("tags", [])) & c["tags"] & set(BREAKING_TAGS)
    if not tags or not it.get("strong"):
        return False
    return bool(set(it["shows"]) & c["shows"]) or bool(set(it["people"]) & c["people"])


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def build_clusters(items: list[dict]) -> list[dict]:
    order = sorted(items, key=lambda i: (i["source_type"] == "gnews", -item_time(i).timestamp()))
    clusters: list[dict] = []
    for it in order:
        tk = tokens(it["title"])
        best, best_score = None, 0.0
        if it["kind"] != "video":
            for c in clusters:
                if c["kind"] == "video":
                    continue
                s = jaccard(tk, c["tokens"])
                shared = set(it["shows"]) & c["shows"]
                if same_event(it, c):
                    s = max(s, 0.99)
                if s >= (0.34 if shared else 0.5) and s > best_score:
                    best, best_score = c, s
        if best:
            best["members"].append(it)
            best["shows"] |= set(it["shows"])
            best["people"] |= set(it["people"])
            best["tags"] |= set(it["tags"])
        else:
            clusters.append({"key": it["id"], "primary": it, "members": [it], "tokens": tk,
                             "kind": it["kind"], "shows": set(it["shows"]),
                             "people": set(it["people"]), "tags": set(it["tags"])})
    for c in clusters:
        finish_cluster(c)
    return clusters


def finish_cluster(c: dict) -> None:
    p = c["primary"]
    seen, sources = set(), []
    for m in sorted(c["members"], key=lambda m: m["source_type"] == "gnews"):
        if m["outlet"] not in seen:
            seen.add(m["outlet"])
            sources.append((m["outlet"], m["link"]))
    c["sources"] = sources
    c["summary"] = next((m["summary"] for m in c["members"] if m.get("summary")), "")
    c["image"] = next((m["image"] for m in [p] + c["members"] if m.get("image")), "")
    if not c["image"] and c["kind"] == "video":  # stories saved before pictures were collected
        vid = re.search(r"[?&]v=([\w-]{6,})", p.get("link", ""))
        if vid:
            c["image"] = f"https://i.ytimg.com/vi/{vid.group(1)}/hqdefault.jpg"
    c["time"] = min(item_time(m) for m in c["members"])
    c["first_seen"] = min(parse_iso(m["first_seen"]) for m in c["members"])
    # videos (talk-show clips, trailers) are never treated as breaking news
    c["strong"] = any(m.get("strong") for m in c["members"])
    c["breaking"] = ([] if c["kind"] == "video" or not c["strong"]
                     else [t for t in BREAKING_TAGS if t in c["tags"]])
    c["sitcom"] = any(m.get("sitcom") for m in c["members"])
    c["relevant"] = any(m.get("relevant") for m in c["members"])
    if c["kind"] == "video":
        kind = p.get("channel_kind")
        if kind == "streamer" or "Trailer" in c["tags"]:
            c["section"] = "trailers"
        else:
            c["section"] = "interviews"
    elif c["breaking"]:
        c["section"] = "breaking"
    elif "Trailer" in c["tags"]:
        c["section"] = "trailers"
    elif "Streaming" in c["tags"]:
        c["section"] = "streaming"
    elif "Upcoming" in c["tags"]:
        c["section"] = "upcoming"
    elif "Interview" in c["tags"]:
        c["section"] = "interviews"
    else:
        c["section"] = "news"


def already_shown(c: dict, shown: list[dict]) -> bool:
    """Is this the same story as one an earlier roundup already listed?"""
    if c["kind"] != "article":
        return False
    ids = {m["id"] for m in c["members"]}
    for e in shown:
        if ids & set(e.get("ids", [])):
            return True
        if jaccard(c["tokens"], set(e.get("tokens", []))) >= 0.5:
            return True
        same_tag = set(c["breaking"]) & set(e.get("tags", []))
        if same_tag and (c["shows"] & set(e.get("shows", [])) or c["people"] & set(e.get("people", []))):
            return True
    return False


def score(c: dict, now: dt.datetime) -> float:
    s = 2.0 * len(c["sources"])
    s += 5 if c["breaking"] else 0
    s += 2 * min(len(c["shows"]) + len(c["people"]), 2)
    s += 1 if c["sitcom"] else 0
    s += 1 if "Trailer" in c["tags"] or "Casting" in c["tags"] else 0
    hours = (now - c["time"]).total_seconds() / 3600
    s += 2 if hours < 6 else 1 if hours < 12 else 0
    s -= 2 if c["kind"] == "video" else 0
    return s


# --------------------------------------------------------------------------
# Packets for Grok
# --------------------------------------------------------------------------

def md_sources(sources: list[tuple[str, str]]) -> str:
    return " · ".join(f"[{o}]({u})" for o, u in sources)


def story_packet(c: dict) -> str:
    """Just the story: the headline as published and the source links.
    Grok's own setup does the writing, so nothing here is reworded."""
    direct = [link for _, link in c["sources"] if "news.google." not in link]
    links = direct[:2] or [c["sources"][0][1]]  # real outlet links first; Google links only as a last resort
    return "\n".join([c["primary"]["title"]] + links)

# --------------------------------------------------------------------------
# Wikidata: birthdays, death anniversaries, premiere and finale anniversaries
# --------------------------------------------------------------------------

WDQS = "https://query.wikidata.org/sparql"
SITCOM_Q = "wd:Q170238"                  # sitcom
COUNTRIES = "wd:Q30 wd:Q145 wd:Q16"      # USA, UK, Canada


def sparql(query: str) -> list[dict]:
    r = http_get(WDQS, params={"query": query, "format": "json"},
                 headers={"Accept": "application/sparql-results+json"}, timeout=90)
    return r.json()["results"]["bindings"]


def _v(row: dict, key: str) -> str:
    return row.get(key, {}).get("value", "")


def _date(s: str) -> dt.date | None:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s or "")
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def person_query(month: int, day: int, dead: bool, limit: int) -> str:
    date_prop = "P570" if dead else "P569"
    alive = "" if dead else "FILTER NOT EXISTS { ?person wdt:P570 ?dod . }"
    other = "OPTIONAL { ?person wdt:P569 ?born . }" if dead else ""
    return f"""
SELECT ?person ?name ?date ?born ?links ?gender ?article (SAMPLE(?img) AS ?image)
       (GROUP_CONCAT(DISTINCT ?showStr; separator="|") AS ?shows) WHERE {{
  VALUES ?country {{ {COUNTRIES} }}
  ?show wdt:P136 {SITCOM_Q} ; wdt:P495 ?country ; wdt:P161 ?person .
  ?person p:{date_prop}/psv:{date_prop} ?node .
  ?node wikibase:timeValue ?date ; wikibase:timePrecision ?prec .
  FILTER(?prec >= 11 && MONTH(?date) = {month} && DAY(?date) = {day})
  {alive}
  {other}
  ?person wikibase:sitelinks ?links .
  ?person rdfs:label ?name . FILTER(LANG(?name) = "en")
  ?show rdfs:label ?showName . FILTER(LANG(?showName) = "en")
  OPTIONAL {{ ?show wdt:P580 ?start . }}
  BIND(IF(BOUND(?start), CONCAT(?showName, "@@", STR(YEAR(?start))), ?showName) AS ?showStr)
  OPTIONAL {{ ?person wdt:P21 ?g . ?g rdfs:label ?gender . FILTER(LANG(?gender) = "en") }}
  OPTIONAL {{ ?article schema:about ?person ; schema:isPartOf <https://en.wikipedia.org/> . }}
  OPTIONAL {{ ?person wdt:P18 ?img . }}
}}
GROUP BY ?person ?name ?date ?born ?links ?gender ?article
ORDER BY DESC(?links)
LIMIT {limit}
"""


def show_query(month: int, day: int, prop: str, limit: int) -> str:
    return f"""
SELECT ?show ?name ?date ?links ?article
       (GROUP_CONCAT(DISTINCT ?netName; separator=", ") AS ?networks) WHERE {{
  VALUES ?country {{ {COUNTRIES} }}
  ?show wdt:P136 {SITCOM_Q} ; wdt:P495 ?country ; p:{prop}/psv:{prop} ?node .
  ?node wikibase:timeValue ?date ; wikibase:timePrecision ?prec .
  FILTER(?prec >= 11 && MONTH(?date) = {month} && DAY(?date) = {day})
  ?show wikibase:sitelinks ?links ; rdfs:label ?name . FILTER(LANG(?name) = "en")
  OPTIONAL {{ ?show wdt:P449 ?net . ?net rdfs:label ?netName . FILTER(LANG(?netName) = "en") }}
  OPTIONAL {{ ?article schema:about ?show ; schema:isPartOf <https://en.wikipedia.org/> . }}
}}
GROUP BY ?show ?name ?date ?links ?article
ORDER BY DESC(?links)
LIMIT {limit}
"""


def parse_shows(raw: str) -> list[str]:
    best: dict[str, int | None] = {}
    for part in raw.split("|"):
        if not part:
            continue
        name, _, year = part.partition("@@")
        y = int(year) if year.isdigit() else None
        if name not in best or (y and (best[name] is None or y < best[name])):
            best[name] = y
    ordered = sorted(best.items(), key=lambda kv: kv[1] or 9999)
    return [f"'{n}' ({y})" if y else f"'{n}'" for n, y in ordered[:4]]


def person_rows(rows: list[dict], today: dt.date, dead: bool) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        qid = _v(r, "person").rsplit("/", 1)[-1]
        when = _date(_v(r, "date"))
        if not when or qid in seen:
            continue
        years = today.year - when.year
        if years <= 0:
            continue
        seen.add(qid)
        out.append({
            "qid": qid,
            "name": _v(r, "name"),
            "date": when,
            "born": _date(_v(r, "born")),
            "years": years,
            "gender": _v(r, "gender"),
            "shows": parse_shows(_v(r, "shows")),
            "wiki": _v(r, "article"),
            "image": commons_image(_v(r, "image")),
        })
    return out


def show_rows(rows: list[dict], today: dt.date) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        qid = _v(r, "show").rsplit("/", 1)[-1]
        when = _date(_v(r, "date"))
        if not when or qid in seen or today.year - when.year <= 0:
            continue
        seen.add(qid)
        out.append({"qid": qid, "name": _v(r, "name"), "date": when,
                    "years": today.year - when.year, "networks": _v(r, "networks"),
                    "wiki": _v(r, "article")})
    return out


def anniversaries(today: dt.date, health: list[dict]) -> dict:
    out: dict = {}
    jobs = [
        ("birthdays", lambda: person_rows(sparql(person_query(today.month, today.day, False, 20)), today, False)),
        ("deaths", lambda: person_rows(sparql(person_query(today.month, today.day, True, 12)), today, True)),
        ("premieres", lambda: show_rows(sparql(show_query(today.month, today.day, "P580", 12)), today)),
        ("finales", lambda: show_rows(sparql(show_query(today.month, today.day, "P582", 12)), today)),
    ]
    for name, fn in jobs:
        try:
            out[name] = fn()
            health.append({"type": "wikidata", "name": name, "ok": True, "count": len(out[name])})
            log(f"  ok   wikidata {name}: {len(out[name])}")
        except Exception as e:
            out[name] = []
            msg = redact(f"{type(e).__name__}: {e}")[:200]
            health.append({"type": "wikidata", "name": name, "ok": False, "count": 0, "error": msg})
            log(f"  FAIL wikidata {name}: {msg}")
        time.sleep(1)
    return out


def links_for(entry: dict) -> list[tuple[str, str]]:
    links = []
    if entry.get("wiki"):
        links.append(("Wikipedia", entry["wiki"]))
    links.append(("Wikidata", f"https://www.wikidata.org/wiki/{entry['qid']}"))
    return links


def birthday_packet(b: dict) -> str:
    lines = [f"{b['name']}, born {us_date(b['date'])}"]
    lines += [u for _, u in links_for(b)[:1]]
    return "\n".join(lines)

def death_packet(d: dict) -> str:
    born = f"born {us_date(d['born'])}, " if d.get("born") else ""
    lines = [f"{d['name']}, {born}died {us_date(d['date'])}"]
    lines += [u for _, u in links_for(d)[:1]]
    return "\n".join(lines)

def show_anniv_packet(s: dict, verb: str) -> str:
    what = "premiered" if verb == "premiered" else "ended"
    net = f" on {s['networks']}" if s["networks"] else ""
    lines = [f"{s['name']} {what} {us_date(s['date'])}{net}"]
    lines += [u for _, u in links_for(s)[:1]]
    return "\n".join(lines)

# --------------------------------------------------------------------------
# TMDB: upcoming comedy premieres
# --------------------------------------------------------------------------

TMDB = "https://api.themoviedb.org/3"
TMDB_SKIP_GENRES = "10767,10764,10763"   # talk, reality, news


def tmdb(path: str, params: dict) -> dict:
    key = os.environ.get("TMDB_API_KEY", "")
    return http_get(TMDB + path, params={"api_key": key, **params}).json()


def upcoming(today: dt.date, health: list[dict]) -> list[dict]:
    if not os.environ.get("TMDB_API_KEY"):
        health.append({"type": "tmdb", "name": "upcoming premieres", "ok": False, "count": 0,
                       "error": "TMDB_API_KEY secret not set, skipped"})
        return []
    out: dict[int, dict] = {}
    base = {"with_genres": "35", "without_genres": TMDB_SKIP_GENRES,
            "with_original_language": "en", "sort_by": "popularity.desc"}
    try:
        # brand-new comedy series in the next 45 days
        new = tmdb("/discover/tv", {**base, "first_air_date.gte": today.isoformat(),
                                    "first_air_date.lte": (today + dt.timedelta(days=45)).isoformat(),
                                    "include_null_first_air_dates": "false"})
        candidates = [(r, True) for r in new.get("results", [])[:15]]
        # returning comedies with episodes in the next 30 days: keep season premieres
        for page in (1, 2, 3):
            ret = tmdb("/discover/tv", {**base, "page": page,
                                        "air_date.gte": today.isoformat(),
                                        "air_date.lte": (today + dt.timedelta(days=30)).isoformat()})
            candidates += [(r, False) for r in ret.get("results", [])]
        for r, is_new in candidates:
            sid = r.get("id")
            if not sid or sid in out:
                continue
            d = tmdb(f"/tv/{sid}", {})
            net_names = [n.get("name", "") for n in d.get("networks", []) if n.get("name")]
            if not net_names or all(n.lower() == "youtube" for n in net_names):
                continue  # web-only or unknown shows are rarely news
            nets = ", ".join(net_names[:2])
            if is_new:
                when = _date(d.get("first_air_date") or r.get("first_air_date") or "")
                label = "new series"
            else:
                nxt = d.get("next_episode_to_air") or {}
                if nxt.get("episode_number") != 1 or (nxt.get("season_number") or 0) < 2:
                    continue
                when = _date(nxt.get("air_date") or "")
                label = f"Season {nxt['season_number']} premiere"
            if not when or when < today:
                continue
            out[sid] = {"id": sid, "name": d.get("name") or r.get("name"), "date": when,
                        "label": label, "networks": nets,
                        "overview": trim(d.get("overview") or "", 260),
                        "image": (TMDB_IMG + "w780" + d["backdrop_path"]) if d.get("backdrop_path")
                        else (TMDB_IMG + "w500" + d["poster_path"]) if d.get("poster_path") else ""}
        res = sorted(out.values(), key=lambda x: x["date"])
        health.append({"type": "tmdb", "name": "upcoming premieres", "ok": True, "count": len(res)})
        log(f"  ok   tmdb upcoming: {len(res)}")
        return res
    except Exception as e:
        msg = redact(f"{type(e).__name__}: {e}")[:200]
        health.append({"type": "tmdb", "name": "upcoming premieres", "ok": False, "count": 0, "error": msg})
        log(f"  FAIL tmdb upcoming: {msg}")
        return sorted(out.values(), key=lambda x: x["date"])


# --------------------------------------------------------------------------
# Pictures for stories that came without one
# --------------------------------------------------------------------------

IMAGES: dict = {}          # cache kept in state["images"]: key -> {"u": url or "", "at": iso}
OG_RE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["'][^>]*>""", re.I)
CONTENT_RE = re.compile(r"""content=["']([^"']+)["']""", re.I)
TMDB_IMG = "https://image.tmdb.org/t/p/"


def _cached(key: str, fn) -> str:
    hit = IMAGES.get(key)
    if hit is not None:
        return hit.get("u", "")
    try:
        url = fn() or ""
    except Exception as e:  # a picture is never worth failing a run over
        log(f"  picture {key[:60]}: {redact(type(e).__name__)}")
        url = ""
    IMAGES[key] = {"u": url, "at": iso(now_utc())}
    return url


def og_image(link: str) -> str:
    """The page's share picture (og:image), read from the first part of the page only."""
    def get():
        r = SESSION.get(link, headers={"User-Agent": "Mozilla/5.0 (compatible; SitcomCraveRoundup/1.0)"},
                        timeout=10, stream=True)
        r.raise_for_status()
        head = b""
        for chunk in r.iter_content(32768):
            head += chunk
            if len(head) > 400_000 or b"</head>" in head:
                break
        r.close()
        text = head.decode("utf-8", "ignore")
        for tag in OG_RE.findall(text):
            m = CONTENT_RE.search(tag)
            if m:
                u = clean_image(m.group(1))
                if u:
                    return u
        return ""
    return _cached("og:" + link, get)


def tmdb_show_image(name: str) -> str:
    if not os.environ.get("TMDB_API_KEY"):
        return ""
    def get():
        res = tmdb("/search/tv", {"query": name}).get("results", [])
        res = [r for r in res if r.get("backdrop_path") or r.get("poster_path")]
        if not res:
            return ""
        exact = [r for r in res if fold(r.get("name", "")).lower() == fold(name).lower()]
        r = max(exact or res[:3], key=lambda r: r.get("popularity") or 0)
        return (TMDB_IMG + "w780" + r["backdrop_path"]) if r.get("backdrop_path") else (TMDB_IMG + "w500" + r["poster_path"])
    return _cached("tmdbtv:" + name.lower(), get)


def tmdb_person_image(name: str) -> str:
    if not os.environ.get("TMDB_API_KEY"):
        return ""
    def get():
        res = [r for r in tmdb("/search/person", {"query": name}).get("results", []) if r.get("profile_path")]
        return TMDB_IMG + "w300" + res[0]["profile_path"] if res else ""
    return _cached("tmdbperson:" + name.lower(), get)


def fill_images(clusters: list[dict], limit: int = 60) -> None:
    """Give pictures to stories whose feeds had none: the article's own share picture first,
    then a TMDB still of the show, then a TMDB photo of the person. Results are cached."""
    from concurrent.futures import ThreadPoolExecutor
    todo = [c for c in clusters if not c.get("image")][:limit]
    if not todo:
        return

    def first_page(c):
        for _, link in c["sources"]:
            if "news.google." not in link:
                u = og_image(link)
                if u:
                    return u
        return ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = list(pool.map(first_page, todo))
    for c, u in zip(todo, found):
        if not u:
            for show in sorted(c["shows"]):
                u = tmdb_show_image(show)
                if u:
                    break
        if not u:
            for person in sorted(c["people"]):
                u = tmdb_person_image(person)
                if u:
                    break
        c["image"] = u
    got = sum(1 for c in todo if c["image"])
    log(f"  pictures: found {got} of {len(todo)} missing")


def commons_image(url: str, width: int = 300) -> str:
    """Wikidata gives Commons 'Special:FilePath' links; ask for a small version."""
    u = clean_image(url)
    return f"{u}?width={width}" if u and "Special:FilePath" in u else u


def upcoming_packet(u: dict) -> str:
    net = f" on {u['networks']}" if u["networks"] else ""
    what = "new series" if u["label"] == "new series" else u["label"]
    return f"{u['name']} ({what}) premieres {us_date(u['date'])}{net}\nhttps://www.themoviedb.org/tv/{u['id']}"

# --------------------------------------------------------------------------
# Telegram alerts
# --------------------------------------------------------------------------

def site_url() -> str:
    explicit = os.environ.get("ROUNDUP_SITE_URL")
    if explicit:
        return explicit.rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return ""


TELEGRAM_FILE = DATA_DIR / "telegram.json"
_TG_CHAT: dict = {}


def telegram_chat(cfg: dict) -> str | None:
    """The chat to alert. Found once from the bot's messages (the user taps Start
    in their bot), then remembered in data/telegram.json."""
    if "id" in _TG_CHAT:
        return _TG_CHAT["id"]
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat and TELEGRAM_FILE.exists():
        try:
            chat = str(json.loads(TELEGRAM_FILE.read_text()).get("chat_id") or "") or None
        except json.JSONDecodeError:
            chat = None
    if not chat and token:
        want = (cfg.get("telegram", {}).get("username") or "").lstrip("@").lower()
        try:
            r = http_get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
            updates = r.json().get("result", [])
            private = {}
            for u in updates:
                c = (u.get("message") or u.get("edited_message") or {}).get("chat") or {}
                if c.get("type") == "private" and c.get("id"):
                    private[str(c["id"])] = (c.get("username") or "").lower()
            match = [cid for cid, name in private.items() if want and name == want]
            if not match and len(private) == 1:
                match = list(private)  # only one person has messaged this bot: that's the owner
            if match:
                chat = match[-1]
                write_text(TELEGRAM_FILE, json.dumps({"chat_id": chat}) + "\n")
                _TG_CHAT["id"] = chat
                send_telegram("Sitcom Crave Roundup is connected. Breaking news and each "
                              "morning and evening roundup will arrive here.", cfg)
                log("  Telegram connected")
            else:
                log(f"  Telegram: {len(updates)} recent messages to the bot, {len(private)} private chats, "
                    "none matched. Open the bot, tap Start and send it any message.")
        except Exception as e:
            log(f"  Telegram lookup failed: {redact(str(e))[:120]}")
    _TG_CHAT["id"] = chat
    return chat


def send_telegram(text: str, cfg: dict | None = None) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = _TG_CHAT.get("id") or (telegram_chat(cfg or {}) if token else None)
    if not token or not chat:
        return False
    try:
        r = SESSION.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"},
                         timeout=20)
        if r.status_code >= 400:
            log(f"  Telegram refused the alert: HTTP {r.status_code}")
            return False
        return True
    except requests.RequestException as e:
        log(f"  Telegram error: {type(e).__name__}")
        return False


def alert_text(c: dict) -> str:
    p = c["primary"]
    kind = {"Death": "In memoriam", "Cancel/End": "Cancelled", "Renewal": "Renewed"}.get(
        c["breaking"][0], c["breaking"][0])
    lines = [f"BREAKING, {kind}: {p['title']}"]
    for outlet, link in c["sources"][:3]:
        lines.append(f"{outlet}: {link}")
    url = site_url()
    if url:
        lines.append(f"Roundup: {url}/breaking.html")
    draft = DRAFTS.get(c["key"], {}).get("text")
    if draft:
        lines += ["", "Draft caption (check before posting):", draft]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Captions: exact templates for On this day, Gemini drafts for news
# --------------------------------------------------------------------------

NETWORK_SHORT = {
    "American Broadcasting Company": "ABC", "National Broadcasting Company": "NBC",
    "Columbia Broadcasting System": "CBS", "CBS Broadcasting": "CBS", "Fox Broadcasting Company": "FOX",
    "Fox": "FOX", "Home Box Office": "HBO", "British Broadcasting Corporation": "BBC",
    "United Paramount Network": "UPN", "Black Entertainment Television": "BET",
}


def short_network(nets: str) -> str:
    return ", ".join(NETWORK_SHORT.get(n.strip(), n.strip()) for n in nets.split(",") if n.strip())


def adjective(gender: str) -> str:
    return "the beautiful" if gender in ("female", "trans woman") else "the talented"


def plural_years(n: int) -> str:
    return f"{n} year{'s' if n != 1 else ''}"


def birthday_caption(b: dict) -> str:
    return f"Happy birthday to {adjective(b['gender'])} {b['name']}, who turns {b['years']} today!"


def death_caption(d: dict) -> str:
    names = [s.rsplit(" (", 1)[0] for s in d["shows"][:2]]
    dates = (f"{short_date(d['born'])} - " if d.get("born") else "") + short_date(d["date"])
    lines = [f"Remembering {adjective(d['gender'])} {d['name']}, who passed away "
             f"{plural_years(d['years'])} ago today. 🕊️", dates]
    if names:
        lines.append(f"Known for: {', '.join(names)}.")
    return "\n".join(lines)


def show_anniv_caption(s: dict, verb: str) -> str:
    net = short_network(s["networks"])
    return f"'{s['name']}' {verb} {plural_years(s['years'])} ago today! {s['date'].year}" + (f", {net}." if net else ".")


DRAFTS: dict = {}   # cluster key -> {"text", "model", "at"}; loaded from state each run
GEMINI = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI: dict = {}


def http_post_json(url: str, body: dict, headers: dict, timeout: int = 60) -> dict:
    r = SESSION.post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}")
    return r.json()


def gemini_models(cfg: dict) -> list[str]:
    """Flash models to try, best first. If one is busy, the next one is used."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return []
    if "models" in _GEMINI:
        return [m for m in _GEMINI["models"] if m not in _GEMINI.setdefault("busy", set())]
    pinned = cfg.get("captions", {}).get("model") or ""
    names: list[str] = []
    try:
        data = http_get(f"{GEMINI}/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}).json()
        names = [m["name"].split("/", 1)[-1] for m in data.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception as e:
        log(f"  Gemini model list failed: {type(e).__name__}")
    skip = ("image", "tts", "live", "audio", "exp", "embedding", "8b", "thinking", "robotics", "computer")
    flash = [n for n in names if "flash" in n and not any(x in n for x in skip)]

    def version(n):
        m = re.search(r"(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0
    full = sorted([n for n in flash if "lite" not in n and "latest" not in n and "preview" not in n], key=version, reverse=True)
    previews = sorted([n for n in flash if "lite" not in n and "preview" in n], key=version, reverse=True)
    lites = sorted([n for n in flash if "lite" in n and "latest" not in n and "preview" not in n], key=version, reverse=True)
    order = ([pinned] if pinned else []) + [n for n in ("gemini-flash-latest",) if n in names] + full[:2] \
        + previews[:1] + [n for n in ("gemini-flash-lite-latest",) if n in names] + lites[:1]
    if not order:
        order = ["gemini-flash-latest"]
    seen, models = set(), []
    for n in order:
        if n not in seen:
            seen.add(n)
            models.append(n)
    _GEMINI["models"] = models
    log(f"  Gemini models to try: {', '.join(models)}")
    return models


def article_text(url: str) -> str:
    """Readable paragraphs of the article, so the draft can stick to what it says."""
    if not url or "news.google." in url:
        return ""
    try:
        page_html = http_get(url, headers={"Accept": "text/html"}, timeout=20).text
    except Exception:
        return ""
    m = re.search(r"<article\b.*?</article>", page_html, re.S | re.I)
    scope = m.group(0) if m else page_html
    scope = re.sub(r"<(script|style|figure|aside|nav|footer)\b.*?</\1>", " ", scope, flags=re.S | re.I)
    paras = [clean_html(p) for p in re.findall(r"<p\b[^>]*>(.*?)</p>", scope, re.S | re.I)]
    text = "\n".join(p for p in paras if len(p) > 40)
    return text[:7000]


def tidy_caption(text: str) -> str:
    text = text.strip().strip("`").strip()
    if len(text) > 1 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    text = re.sub(r"https?://\S+", "", text)                 # captions carry no links
    text = text.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\s*\n\s*\n\s*(\(via [^)]*\))\s*$", r" \1", text)  # keep the credit on the last line
    return text.strip()[:700]


def draft_caption(c: dict, cfg: dict) -> str | None:
    """A Gemini draft in the house style, built only from the article itself."""
    if c["key"] in DRAFTS:
        return DRAFTS[c["key"]]["text"]
    models = gemini_models(cfg)
    if not models or c["kind"] != "article":
        return None
    key = os.environ["GEMINI_API_KEY"]
    rules_path = CONFIG_DIR / "caption_rules.md"
    if not rules_path.exists():
        rules_path = Path(__file__).resolve().parent / "config" / "caption_rules.md"
    rules = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
    p = c["primary"]
    direct = [(o, u) for o, u in c["sources"] if "news.google." not in u]
    outlet, link = direct[0] if direct else c["sources"][0]
    text = article_text(link)
    pub = parse_iso(p.get("published"))
    prompt = "\n".join([
        "Write the caption for this story.",
        f"Headline: {p['title']}",
        f"Outlet: {outlet}",
        f"Published: {us_date(pub.date()) if pub else 'recently'}",
        f"Summary: {c['summary'] or '(none)'}",
        "Article text:",
        text or "(not available: use only the headline and summary)",
    ])
    body = {
        "systemInstruction": {"parts": [{"text": rules}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
    }
    for model in models:
        for attempt in range(2):
            try:
                data = http_post_json(f"{GEMINI}/models/{model}:generateContent", body,
                                      {"x-goog-api-key": key, "Content-Type": "application/json"})
                parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                out = tidy_caption("".join(part.get("text", "") for part in parts if not part.get("thought")))
                if not out:
                    break  # empty answer: try the next model
                DRAFTS[c["key"]] = {"text": out, "model": model, "at": iso(now_utc()),
                                    "from_article": bool(text)}
                return out
            except Exception as e:
                msg = str(e) if isinstance(e, FetchError) else type(e).__name__  # FetchError holds only "HTTP nnn"
                busy = any(code in msg for code in ("429", "500", "503", "504"))
                if busy and attempt == 0:
                    time.sleep(8)
                    continue
                log(f"  Gemini {model} failed: {redact(msg)}")
                if busy or "404" in msg:
                    _GEMINI.setdefault("busy", set()).add(model)  # skip it for the rest of this run
                break
    return None


def draft_many(clusters: list[dict], cfg: dict, limit: int = 12) -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        return 0
    made = 0
    for c in clusters[:limit]:
        if c["key"] in DRAFTS or c["kind"] != "article":
            continue
        if not gemini_models(cfg):
            log("  every Gemini model is busy right now; drafts will be tried again next run")
            break
        if draft_caption(c, cfg):
            made += 1
            time.sleep(float(cfg.get("captions", {}).get("pause_seconds", 5)))
    log(f"  {made} new caption drafts")
    return made


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

E = html.escape

CSS = """
@font-face{font-family:"Archivo";src:url("FONTPATH") format("woff2-variations"),url("FONTPATH") format("woff2");font-weight:100 900;font-stretch:62% 125%;font-style:normal;font-display:swap}
:root{
--paper:#fff;--card:#fff;--soft:#f3f5f7;--ink:#081823;--slate:#5a6876;--rule:#e3e7eb;--crave:#ff751f;--crave-ink:#c2410c;--pink:#d6336c;
--pick:#fff2e8;--bar:#081823;--red:#d92d20;--red-soft:#fdecea;--red-ink:#9b1c14;--green:#14804a;--tile:#0d2433;
--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:24px;--s6:32px;--s7:40px;--s8:48px;
--t1:13px;--t2:15px;--t3:17px;--t4:21px;--t5:28px;--t6:40px;
--r:8px;--r2:14px;--narrow:68%;--col:760px;--nav:0px}
@media (prefers-color-scheme:dark){:root{--paper:#061219;--card:#0b1c27;--soft:#102330;--ink:#eef2f5;--slate:#93a3b1;--rule:#1b2f3d;--crave-ink:#ff8f4d;--pink:#ff7aa8;--pick:#1d2a33;--bar:#0e1f2a;--red:#ff5a4e;--red-soft:#3a1512;--red-ink:#ffb4ad;--tile:#12303f}}
@media (max-width:720px){:root{--nav:60px}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:"Archivo",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:var(--t3);line-height:1.5}
a{color:inherit;text-decoration-color:var(--rule);text-underline-offset:2px}
.wrap{max-width:var(--col);margin:0 auto;padding-left:max(var(--s4),env(safe-area-inset-left));padding-right:max(var(--s4),env(safe-area-inset-right))}
main{padding-bottom:calc(var(--s8) + var(--nav) + env(safe-area-inset-bottom))}
body.has-sel main{padding-bottom:calc(150px + var(--nav) + env(safe-area-inset-bottom))}
input.pick:focus-visible,button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible{outline:2px solid var(--crave);outline-offset:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* header */
header.top{position:sticky;top:0;z-index:8;background:var(--paper);border-bottom:1px solid var(--rule);padding-top:env(safe-area-inset-top)}
.mast{display:flex;align-items:center;justify-content:space-between;gap:var(--s3);padding-top:var(--s3);padding-bottom:var(--s3)}
.brand{display:inline-flex;align-items:center;gap:var(--s3);font-stretch:var(--narrow);font-weight:900;font-size:var(--t4);line-height:1;letter-spacing:-.01em;text-decoration:none}
.brand img{width:36px;height:36px;border-radius:var(--r);display:block}
.tabs{display:flex;gap:var(--s5);font-size:var(--t2)}
.tabs a{position:relative;text-decoration:none;color:var(--slate);padding:var(--s2) 0;font-weight:600}
.tabs a.on{color:var(--ink);box-shadow:inset 0 -2px 0 var(--crave)}
.dot{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;padding:0 5px;margin-left:6px;border-radius:9px;background:var(--red);color:#fff;font-size:11px;font-weight:800;line-height:1;vertical-align:2px}
.dot[hidden]{display:none}

/* filter chips */
.chipbar{border-top:1px solid var(--rule)}
.chips{display:flex;gap:var(--s2);overflow-x:auto;scrollbar-width:none;padding-top:var(--s2);padding-bottom:var(--s2);-webkit-overflow-scrolling:touch}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:0 var(--s3);border:1px solid var(--rule);border-radius:17px;background:var(--card);color:var(--ink);font:inherit;font-size:var(--t1);font-weight:700;cursor:pointer;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.chip span{color:var(--slate);font-weight:500}
.chip.on{background:var(--ink);border-color:var(--ink);color:var(--paper)}
.chip.on span{color:var(--paper);opacity:.75}
.chip.hot{border-color:var(--red);color:var(--red)}
.chip.hot.on{background:var(--red);border-color:var(--red);color:#fff}
.chip svg{width:16px;height:16px}
.searchrow{padding-bottom:var(--s2)}
.searchrow[hidden]{display:none}
.searchrow input{width:100%;font:inherit;font-size:16px;min-height:40px;padding:0 var(--s3);border:1px solid var(--rule);border-radius:var(--r);background:var(--card);color:var(--ink)}

/* intro */
.intro{padding:var(--s5) 0 var(--s2)}
.intro h1{font-stretch:var(--narrow);font-weight:900;font-size:var(--t6);line-height:1;margin:0;letter-spacing:-.015em}
.intro p{margin:var(--s2) 0 0;color:var(--slate);font-size:var(--t2);max-width:36em}

/* breaking banner */
#bbanner{margin:var(--s4) 0 0;border-radius:var(--r2);background:var(--red-soft);color:var(--red-ink);overflow:hidden}
#bbanner[hidden]{display:none}
#bbanner a.bb{display:flex;gap:var(--s3);align-items:flex-start;padding:var(--s3) var(--s4);text-decoration:none}
#bbanner a.bb + a.bb{border-top:1px solid rgba(217,45,32,.18)}
.bb-tag{flex:none;margin-top:2px;padding:2px 8px;border-radius:6px;background:var(--red);color:#fff;font-size:11px;font-weight:800;letter-spacing:.02em;text-transform:uppercase}
.bb-t{font-weight:700;font-size:var(--t2);line-height:1.35}
.bb-when{display:block;font-weight:500;font-size:var(--t1);opacity:.8}
.bb-more{display:block;padding:var(--s2) var(--s4) var(--s3);font-size:var(--t1);font-weight:700;text-decoration:underline}

/* sections */
section.sec{scroll-margin-top:120px}
body.filtering section.sec.off{display:none}
h2{font-stretch:var(--narrow);font-weight:800;font-size:var(--t5);line-height:1.1;margin:var(--s7) 0 var(--s2);padding-bottom:var(--s2);border-bottom:2px solid var(--ink);display:flex;align-items:baseline;gap:var(--s3)}
h2 .count{font-stretch:100%;font-weight:500;font-size:var(--t2);color:var(--slate)}
h3{font-size:var(--t1);font-weight:800;letter-spacing:.04em;text-transform:uppercase;margin:var(--s5) 0 var(--s1);color:var(--slate)}
.muted{color:var(--slate);font-size:var(--t2)}
.empty{color:var(--slate);font-size:var(--t2);margin:var(--s3) 0}

/* story rows */
.item{position:relative;display:grid;grid-template-columns:96px minmax(0,1fr) 24px;column-gap:var(--s3);padding:var(--s3) 0;border-bottom:1px solid var(--rule);cursor:pointer;-webkit-tap-highlight-color:transparent;scroll-margin-top:120px}
.item.noimg{grid-template-columns:minmax(0,1fr) 24px}
.item.sel{background:var(--pick);box-shadow:calc(-1 * var(--s3)) 0 0 var(--pick),var(--s3) 0 0 var(--pick)}
.item.sel::before{content:"";position:absolute;left:calc(-1 * var(--s3));top:0;bottom:0;width:4px;background:var(--crave)}
.item.gone,body.filtering .item.nomatch{display:none}
body.show-hidden .item.gone{display:grid;opacity:.45}
.thumb{position:relative;width:96px;height:72px;border-radius:var(--r);overflow:hidden;background:var(--tile)}
.thumb img{display:block;width:100%;height:100%;object-fit:cover}
.thumb.face img{object-position:50% 18%}
.thumb.ph{display:flex;align-items:flex-end;padding:6px 8px}
.thumb.ph span{color:var(--crave);font-stretch:var(--narrow);font-weight:900;font-size:15px;line-height:1.05;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
.thumb .play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
.thumb .play i{width:30px;height:30px;border-radius:50%;background:rgba(8,24,35,.72);display:flex;align-items:center;justify-content:center}
.thumb .play i::after{content:"";margin-left:3px;border-left:10px solid #fff;border-top:6px solid transparent;border-bottom:6px solid transparent}
.body{min-width:0}
.kick{font-size:var(--t1);line-height:1.3;margin-bottom:2px;color:var(--slate)}
.kick .tag{margin-right:6px}
.t{display:block;font-weight:700;font-size:var(--t3);line-height:1.3;cursor:pointer}
.sum{color:var(--slate);font-size:var(--t2);line-height:1.45;margin-top:var(--s1);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.meta{font-size:var(--t1);color:var(--slate);margin-top:var(--s1)}
.meta a{color:var(--ink)}
.meta details{display:inline}
.meta summary{display:inline;cursor:pointer;list-style:none;text-decoration:underline;text-decoration-color:var(--rule);text-underline-offset:2px}
.meta summary::-webkit-details-marker{display:none}
input.pick{-webkit-appearance:none;appearance:none;width:24px;height:24px;margin:2px 0 0;border:2px solid var(--ink);border-radius:7px;background:transparent;cursor:pointer;opacity:.45}
input.pick:checked{opacity:1;border-color:var(--crave);background:var(--crave) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M5 12.5l4.5 4.5L19 7.5' fill='none' stroke='white' stroke-width='3.2' stroke-linecap='square'/%3E%3C/svg%3E") center/16px no-repeat}
.item > .pick{grid-column:-2}
.acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:var(--s2)}
.act{display:inline-flex;align-items:center;gap:5px;min-height:30px;padding:0 10px;border:1px solid var(--rule);border-radius:15px;background:transparent;color:var(--ink);font:inherit;font-size:12px;font-weight:700;cursor:pointer;-webkit-tap-highlight-color:transparent}
.act svg{width:14px;height:14px;flex:none}
.act.on{background:var(--soft)}
.act.quiet{color:var(--slate)}
.act[disabled]{opacity:.7;cursor:progress}
.act.busy::after{content:"";width:11px;height:11px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.act.busy::after{animation:none}}
.panel{cursor:auto}
.item > .acts,.item > .panel{grid-column:1 / -1;min-width:0}
.item > .pick{grid-row:1}
.today .item > .acts,.today .item > .panel{grid-column:2 / -1}
.today .item.next > .acts,.today .item.next > .panel{grid-column:1 / -1}
.panel[hidden]{display:none}
.writer-out:empty{display:none}
.posted-badge{display:inline-flex;align-items:center;gap:6px;margin:0 6px 0 0;padding:0 8px;min-height:22px;border:0;border-radius:11px;background:var(--green);color:#fff;font:inherit;font-size:12px;font-weight:700;cursor:pointer;vertical-align:baseline}
.posted-badge::before{content:"";width:8px;height:4px;border-left:2px solid #fff;border-bottom:2px solid #fff;transform:rotate(-45deg) translateY(-2px)}
.item.posted .t{color:var(--slate)}
.item.posted .thumb{opacity:.55}

/* caption boxes */
.cap{margin:var(--s2) 0 0;padding:var(--s3);border:1px solid var(--rule);border-radius:var(--r);background:var(--card);cursor:auto}
.cap-h{font-size:var(--t1);color:var(--slate);margin-bottom:var(--s1)}
.cap-t{margin:0 0 var(--s3);font-size:var(--t2);white-space:pre-line;-webkit-user-select:text;user-select:text}
.capcopy,.writecap{display:inline-flex;align-items:center;gap:var(--s2);min-height:40px;padding:0 var(--s4);font:inherit;font-size:var(--t2);font-weight:700;border:1px solid var(--ink);border-radius:var(--r);background:transparent;color:var(--ink);cursor:pointer}
.capcopy.done{background:var(--crave);border-color:var(--crave);color:#081823}
.writecap.primary{background:var(--crave);border-color:var(--crave);color:#081823;min-height:48px;padding:0 var(--s5)}
.writecap[disabled]{opacity:.7;cursor:progress}
.writecap.busy::after{content:"";width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
.note{font-size:var(--t1);color:var(--slate);margin-top:var(--s2)}
.note a{color:var(--ink)}

/* not sitcom panel */
.mute{margin-top:var(--s2);padding:var(--s3);border-radius:var(--r);background:var(--soft);font-size:var(--t1)}
.mute p{margin:0 0 var(--s2);color:var(--slate)}
.mute .acts{margin-top:0}
.mute .act{background:var(--card)}

/* schedule */
.sched-top{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:var(--s2);font-size:var(--t1);color:var(--slate)}
.sched-top b{color:var(--ink)}
.progress{height:6px;border-radius:3px;background:var(--soft);margin:var(--s2) 0 var(--s1);overflow:hidden}
.progress i{display:block;height:100%;width:0;background:var(--crave);border-radius:3px;transition:width .3s ease-out}
.today .item{grid-template-columns:44px 96px minmax(0,1fr) 24px}
.today .item.noimg{grid-template-columns:44px minmax(0,1fr) 24px}
.time{font-stretch:62%;font-weight:900;font-size:var(--t4);line-height:1;color:var(--crave-ink);letter-spacing:-.01em;padding-top:2px}
.time small{font-size:12px;font-weight:700}
.today .item.next{grid-template-columns:minmax(0,1fr) 24px;margin:var(--s3) 0;padding:var(--s3);border:2px solid var(--crave);border-radius:var(--r2);background:var(--card)}
.today .item.next.sel{background:var(--pick);box-shadow:none}
.today .item.next.sel::before{display:none}
.today .item.next .time{grid-column:1;grid-row:1;font-size:var(--t1);font-stretch:100%;font-weight:800;color:var(--crave-ink);padding:0 0 var(--s2)}
.today .item.next .time small{font-size:var(--t1)}
.today .item.next .time::before{content:"Up next, "}
.today .item.next .thumb{grid-column:1 / -1;grid-row:2;width:100%;height:auto;aspect-ratio:16/9;margin-bottom:var(--s3);border-radius:var(--r)}
.today .item.next .thumb.ph{aspect-ratio:auto;height:88px;align-items:center;padding:0 var(--s4)}
.today .item.next .thumb.ph span{font-size:var(--t5);-webkit-line-clamp:1}
.today .item.next .body{grid-column:1 / -1;grid-row:3}
.today .item.next > .pick{grid-column:2;grid-row:1}
.today .item.next .t{font-size:var(--t4);line-height:1.25}
.next-when{color:var(--crave-ink);font-weight:800}

/* settings */
#settings{margin:var(--s8) 0 0;border-top:2px solid var(--ink);padding-top:var(--s1)}
#settings > summary{cursor:pointer;font-stretch:var(--narrow);font-weight:800;font-size:var(--t4);padding:var(--s2) 0;list-style:none}
#settings > summary::-webkit-details-marker{display:none}
#settings label{display:flex;gap:var(--s3);align-items:center;padding:var(--s2) 0;font-size:var(--t2)}
#settings input[type=checkbox]{width:22px;height:22px;margin:0;accent-color:var(--crave)}
.setblock{margin-top:var(--s4)}
.setblock b{display:block;font-size:var(--t2);margin-bottom:var(--s1)}
textarea{width:100%;font:inherit;font-size:16px;padding:var(--s3);border:1px solid var(--rule);border-radius:var(--r);background:var(--card);color:var(--ink);margin-top:var(--s2)}
.keyrow{display:flex;gap:var(--s2);align-items:center;margin:var(--s2) 0}
.keyrow input{flex:1;min-width:0;font:inherit;font-size:16px;padding:var(--s2) var(--s3);min-height:40px;border:1px solid var(--rule);border-radius:var(--r);background:var(--card);color:var(--ink)}
.linkish{background:none;border:0;font:inherit;font-size:var(--t2);color:var(--slate);text-decoration:underline;cursor:pointer;padding:var(--s1)}
#mutelist{display:flex;flex-wrap:wrap;gap:6px;margin-top:var(--s2)}
.maker{margin:var(--s5) 0 0}
.maker-label{display:block;font-weight:700;font-size:var(--t2);margin-bottom:var(--s1)}
.maker textarea{margin:0 0 var(--s3);min-height:144px}
.maker-actions{display:flex;align-items:center;gap:var(--s4)}
.maker .writer-out .cap{margin-top:var(--s5)}
.maker .writer-out .cap-t{font-size:var(--t3)}

/* action tray (shows when stories are picked) */
#tray{position:fixed;left:0;right:0;bottom:calc(var(--nav) + env(safe-area-inset-bottom));z-index:10;background:var(--bar);color:#fff;border-top:4px solid var(--crave);padding:var(--s3) max(var(--s4),env(safe-area-inset-right)) var(--s3) max(var(--s4),env(safe-area-inset-left));transform:translateY(calc(100% + var(--nav) + 40px));transition:transform .18s ease-out}
@media (min-width:721px){#tray{padding-bottom:calc(var(--s3) + env(safe-area-inset-bottom));bottom:0}}
body.has-sel #tray{transform:none}
#tray .in{max-width:var(--col);margin:0 auto}
#tray .info{display:flex;align-items:baseline;gap:var(--s3);margin-bottom:var(--s2);min-height:24px}
#tray .info b{font-stretch:var(--narrow);font-weight:900;font-size:var(--t4);white-space:nowrap}
#status{color:#ffd2b5;font-size:var(--t1);flex:1;line-height:1.3}
#tray .linkbtn{background:none;border:0;color:#c9d3dc;font:inherit;font-size:var(--t2);text-decoration:underline;text-underline-offset:3px;padding:var(--s1) 0}
#tray .btns{display:grid;grid-template-columns:repeat(auto-fit,minmax(72px,1fr));gap:var(--s2)}
#tray .btns button{min-height:46px;font:inherit;font-size:var(--t2);font-weight:700;border-radius:var(--r);border:1px solid rgba(255,255,255,.35);background:transparent;color:#fff;padding:var(--s1) var(--s2);-webkit-tap-highlight-color:transparent}
#tray .btns button.primary{background:var(--crave);border-color:var(--crave);color:#081823}
#tray .btns button[hidden]{display:none}

/* bottom tab bar on phones */
nav.bottom{display:none}
@media (max-width:720px){
.tabs{display:none}
nav.bottom{display:grid;grid-template-columns:repeat(4,1fr);position:fixed;left:0;right:0;bottom:0;z-index:11;height:calc(var(--nav) + env(safe-area-inset-bottom));padding-bottom:env(safe-area-inset-bottom);background:var(--paper);border-top:1px solid var(--rule)}
nav.bottom a{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;text-decoration:none;color:var(--slate);font-size:11px;font-weight:700;-webkit-tap-highlight-color:transparent}
nav.bottom a svg{width:24px;height:24px}
nav.bottom a.on{color:var(--crave-ink)}
nav.bottom .dot{position:absolute;top:6px;left:calc(50% + 6px);margin:0}
.intro h1{font-size:34px}
}
@media (prefers-reduced-motion:reduce){#tray,.progress i{transition:none}}
@media (min-width:900px){
.item{grid-template-columns:132px minmax(0,1fr) 24px;column-gap:var(--s4);padding:var(--s4) 0}
.thumb{width:132px;height:88px}
.today .item{grid-template-columns:52px 132px minmax(0,1fr) 24px}
.today .item.next{grid-template-columns:minmax(0,1fr) 24px;padding:var(--s4)}
.today .item.next .thumb:not(.ph){width:100%;height:auto}
}

/* sources + archive */
.health{margin-top:var(--s3)}
.health > summary{cursor:pointer;padding:var(--s2) 0;color:var(--slate);font-size:var(--t2)}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:var(--s3)}
table{border-collapse:collapse;font-size:var(--t1)}
td,th{border-bottom:1px solid var(--rule);padding:var(--s1) var(--s3) var(--s1) 0;text-align:left;vertical-align:top}
.arch{display:flex;align-items:center;gap:var(--s4);padding:var(--s3) var(--s4);margin-top:var(--s2);border:1px solid var(--rule);border-radius:var(--r2);background:var(--card);text-decoration:none}
.arch b{font-stretch:var(--narrow);font-weight:800;font-size:var(--t4);min-width:4.5em}
.arch span{color:var(--slate);font-size:var(--t2)}
.arch span:last-child{margin-left:auto}
.archday{margin-top:var(--s6)}

.tag{font-weight:800;color:var(--slate)}
.k-renewed{color:#14804a}.k-cancelled{color:#c8231a}.k-memoriam{color:var(--ink)}
.k-exit{color:var(--crave-ink)}.k-arrest{color:#b4234f}.k-revival,.k-trailer{color:#6c3fc4}
.k-casting{color:#1d5fd1}.k-streaming{color:#3b4a9c}.k-interview,.k-clip{color:#0b7285}
.k-upcoming,.k-premiere{color:#1d5fd1}.k-birthday{color:var(--pink)}.k-anniv{color:var(--crave-ink)}
@media (prefers-color-scheme:dark){.k-renewed{color:#4cc38a}.k-cancelled{color:#ff6b61}.k-arrest{color:#ff7aa0}.k-revival,.k-trailer{color:#b197fc}.k-casting,.k-upcoming,.k-premiere{color:#74a7ff}.k-streaming{color:#a5b4fc}.k-interview,.k-clip{color:#5fd4e8}}
"""

TRAY = """
<div id="tray" aria-live="polite"><div class="in">
<div class="info"><b><span id="count">0</span> picked</b><span id="status"></span><button id="clear" class="linkbtn">Clear</button></div>
<div class="btns">
<button id="copy" class="primary">Copy</button>
<button id="open">Open Grok</button>
<button id="share" hidden>Share</button>
<button id="markposted">Posted</button>
</div>
</div></div>
"""

SETTINGS = """
<details id="settings"><summary>Settings</summary>
<div class="setblock"><b>Hidden stories</b>
<div class="muted" id="hidden-state">Stories you mark "Not sitcom" are hidden on this device.</div>
<label><input type="checkbox" id="showhidden"> Show hidden stories</label>
<div class="muted">Muted words and shows (any story with these in the headline is hidden):</div>
<div id="mutelist"></div></div>
<div class="setblock"><b>Gemini key</b>
<div class="muted">For writing captions on this device. It's saved only on this phone or computer, never on the site:</div>
<div class="keyrow"><input id="gemkey" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Paste your Gemini API key">
<button type="button" id="gemkey-save" class="writecap">Save</button><button type="button" id="gemkey-clear" class="linkish">Remove</button></div>
<div class="muted" id="gemkey-state"></div></div>
<div class="setblock"><b>Line above stories sent to Grok</b>
<div class="muted">Optional. Leave empty to send only the stories (saved on this device):</div>
<textarea id="header" rows="3"></textarea></div>
<p class="muted">Tap a story to pick it. Stories you mark as posted get a green Posted badge; tap the badge to undo. Share opens the iPhone share sheet, so you can send the packet straight to the Grok app. Open Grok opens grok.com with it typed in. Both also copy it.</p>
</details>
"""

MAKER = """
<div id="maker" class="maker">
<label for="maker-in" class="maker-label">Link or story text</label>
<textarea id="maker-in" rows="6" placeholder="Paste a link to any story, or paste the story's text"></textarea>
<div class="maker-actions"><button type="button" id="maker-go" class="writecap primary">Write caption</button>
<button type="button" id="maker-clear" class="linkish">Clear</button></div>
<div id="maker-out" class="writer-out"></div>
</div>
"""

_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>'
ICONS = {
    "home": _SVG.format('<path d="M4 11l8-7 8 7v9a1 1 0 0 1-1 1h-5v-6h-4v6H5a1 1 0 0 1-1-1z"/>'),
    "bolt": _SVG.format('<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>'),
    "pen": _SVG.format('<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>'),
    "archive": _SVG.format('<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8M10 12h4"/>'),
    "search": _SVG.format('<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>'),
    "spark": _SVG.format('<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>'),
    "doc": _SVG.format('<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M10 12h5M10 16h5"/>'),
    "eyeoff": _SVG.format('<path d="M3 3l18 18M10.6 6.1A9.8 9.8 0 0 1 12 6c5 0 9 6 9 6a16 16 0 0 1-2.6 3.2M6.6 6.6C4.3 8.1 3 12 3 12s4 6 9 6a9 9 0 0 0 4.4-1.1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'),
}

_RULES: dict = {}


def caption_rules_text() -> str:
    if "text" not in _RULES:
        path = CONFIG_DIR / "caption_rules.md"
        if not path.exists():
            path = Path(__file__).resolve().parent / "config" / "caption_rules.md"
        _RULES["text"] = path.read_text(encoding="utf-8") if path.exists() else ""
    return _RULES["text"]


# small coloured word before a headline: (text shown, colour class)
KICKERS = {
    "Death": ("In memoriam", "memoriam"), "Cancel/End": ("Cancelled", "cancelled"),
    "Renewal": ("Renewed", "renewed"), "Exit": ("Exit", "exit"), "Arrest": ("Arrest", "arrest"),
    "Revival": ("Revival", "revival"), "Casting": ("Casting", "casting"),
    "Streaming": ("Streaming", "streaming"), "Interview": ("Interview", "interview"),
    "Trailer": ("Trailer", "trailer"), "Upcoming": ("Coming up", "upcoming"), "Clip": ("Clip", "clip"),
    "Birthday": ("Birthday", "birthday"), "Remembering": ("Remembering", "memoriam"),
    "Premiere": ("Premiere", "premiere"), "Anniversary": ("Anniversary", "anniv"),
    "Finale": ("Finale", "anniv"), "News": ("", ""),
}


def kicker(label: str) -> str:
    text, cls = KICKERS.get(label, (label, ""))
    return f'<span class="tag k-{cls}">{E(text)}</span>' if text else ""


def chip_for(c: dict) -> str:
    if c["breaking"]:
        return c["breaking"][0]
    if c["kind"] == "video":
        return "Trailer" if c["section"] == "trailers" else "Clip"
    if "Casting" in c["tags"]:
        return "Casting"
    return SECTION_TYPE[c["section"]]


DEFAULT_DESC = "Comedy TV news for Sitcom Crave, gathered every morning and evening."
TABS = (("latest", "Latest", "Today", "index.html", "home"), ("breaking", "Breaking", "Breaking", "breaking.html", "bolt"),
        ("captions", "Captions", "Captions", "captions.html", "pen"), ("archive", "Archive", "Archive", "archive.html", "archive"))


def breaking_feed(clusters: list[dict]) -> list[dict]:
    """The small list the breaking banner and the red dots are drawn from."""
    out = []
    for c in clusters[:12]:
        out.append({"id": c["key"], "title": c["primary"]["title"], "url": c["sources"][0][1] if c["sources"] else "",
                    "outlet": c["sources"][0][0] if c["sources"] else "", "tag": KICKERS.get(c["breaking"][0], ("",))[0],
                    "time": iso(c["time"]), "seen": iso(c["first_seen"])})
    return out


def page(title: str, body: str, depth: int = 0, chips: list[tuple[str, str, int | None]] | None = None,
         active: str = "latest", desc: str = "", breaking: list[dict] | None = None, banner: bool = False) -> str:
    pre = "../" * depth
    desc = desc or DEFAULT_DESC
    site = site_url()
    og_image = (f'<meta property="og:image" content="{E(site)}/og-image.jpg">\n'
                f'<meta property="og:image:width" content="1200">\n<meta property="og:image:height" content="630">\n'
                f'<meta property="og:image:alt" content="Sitcom Crave">\n'
                f'<meta name="twitter:image" content="{E(site)}/og-image.jpg">\n'
                if site else "")
    def dot(key):
        return '<span class="dot bdot" hidden></span>' if key == "breaking" else ""
    tabs = "".join(f'<a href="{pre}{href}"{" class=\"on\"" if key == active else ""}>{label}{dot(key)}</a>'
                   for key, label, _, href, _ in TABS)
    bottom = "".join(f'<a href="{pre}{href}"{" class=\"on\" aria-current=\"page\"" if key == active else ""}>'
                     f'{ICONS[icon]}<span>{short}</span>{dot(key)}</a>' for key, _, short, href, icon in TABS)
    mast = (f'<div class="mast wrap"><a class="brand" href="{pre}index.html">'
            f'<img src="{pre}logo-96.png" width="36" height="36" alt="Sitcom Crave"><span>Roundup</span></a>'
            f'<nav class="tabs" aria-label="Pages">{tabs}</nav></div>')
    bar = ""
    if chips:
        items = ['<button type="button" class="chip on" data-filter="all">All</button>']
        for key, label, n in chips:
            hot = " hot" if key == "breaking" else ""
            items.append(f'<button type="button" class="chip{hot}" data-filter="{E(key)}">{E(label)}'
                         + (f"<span>{n}</span>" if n else "") + "</button>")
        items.append(f'<button type="button" class="chip" id="searchbtn" aria-label="Search stories" aria-expanded="false">{ICONS["search"]}</button>')
        bar = (f'<div class="chipbar"><div class="wrap"><div class="chips" role="toolbar" aria-label="Show sections">{"".join(items)}</div>'
               f'<div class="searchrow" id="searchrow" hidden><input id="search" type="search" placeholder="Search headlines" '
               f'autocomplete="off" aria-label="Search headlines"></div></div></div>')
    css = CSS.replace("FONTPATH", f"{pre}fonts/archivo.woff2")
    rules_js = json.dumps(caption_rules_text()).replace("</", "<\\/")  # safe inside <script>
    bjs = json.dumps(breaking if breaking is not None else None).replace("</", "<\\/")
    bdiv = '<div id="bbanner" hidden></div>' if banner else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="format-detection" content="telephone=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Roundup">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#061219" media="(prefers-color-scheme: dark)">
<meta name="referrer" content="no-referrer">
<link rel="preload" href="{pre}fonts/archivo.woff2" as="font" type="font/woff2" crossorigin>
<link rel="icon" href="{pre}favicon.ico?v=3" sizes="any">
<link rel="icon" type="image/png" sizes="16x16" href="{pre}favicon-16.png?v=3">
<link rel="icon" type="image/png" sizes="32x32" href="{pre}favicon-32.png?v=3">
<link rel="icon" type="image/png" sizes="48x48" href="{pre}favicon-48.png?v=3">
<link rel="icon" type="image/png" sizes="192x192" href="{pre}icon-192.png?v=3">
<link rel="apple-touch-icon" sizes="180x180" href="{pre}icon-180.png?v=3">
<link rel="manifest" href="{pre}site.webmanifest?v=3">
<title>{E(title)}</title>
<meta name="description" content="{E(desc)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Sitcom Crave Roundup">
<meta property="og:title" content="{E(title)}">
<meta property="og:description" content="{E(desc)}">
{og_image}<meta name="twitter:card" content="summary_large_image">
<style>{css}</style>
<script>window.ROUNDUP_DEFAULT_HEADER = {json.dumps(DEFAULT_HEADER)};
window.ROUNDUP_CAPTION_RULES = {rules_js};
window.ROUNDUP_BREAKING = {bjs};
window.ROUNDUP_ROOT = {json.dumps(pre)};</script>
</head><body>
<header class="top">{mast}{bar}</header>
<main class="wrap">
{bdiv}
{body}
{SETTINGS}
</main>
<nav class="bottom" aria-label="Pages">{bottom}</nav>
{TRAY}
<script src="{pre}app.js?v=4"></script>
</body></html>
"""


def section(key: str, label: str, count: int | None, inner: str) -> str:
    n = f'<span class="count">{count}</span>' if count else ""
    return (f'<section class="sec" id="sec-{E(key)}" data-sec="{E(key)}">'
            f'<h2>{E(label)}{n}</h2>\n{inner}\n</section>')


def links_html(sources: list[tuple[str, str]], show: int = 3) -> str:
    def a(o, u):
        return f'<a href="{E(u)}" target="_blank" rel="noopener">{E(o)}</a>'
    names = [a(o, u) for o, u in sources]
    if len(names) <= 1:
        return "".join(names)
    if len(names) <= show:
        return ", ".join(names[:-1]) + " and " + names[-1]
    rest = names[show:]
    return (", ".join(names[:show]) + f' <details><summary>and {len(rest)} more</summary>, '
            + ", ".join(rest) + "</details>")


def caption_html(text: str, note: str) -> str:
    return (f'<div class="cap" data-cap="{E(text)}"><div class="cap-h">{E(note)}</div>'
            f'<p class="cap-t">{E(text)}</p>'
            f'<button type="button" class="capcopy">Copy caption</button></div>')


def draft_caption_html(key: str) -> str:
    d = DRAFTS.get(key)
    if not d:
        return ""
    note = ("Draft caption by Gemini. Check it against the source before posting."
            if d.get("from_article", True) else
            "Draft caption by Gemini from the headline only (the article couldn't be read). Check it carefully.")
    return caption_html(d["text"], note)


def thumb_html(image: str, ph: str, *, face: bool = False, video: bool = False) -> str:
    play = '<span class="play"><i></i></span>' if video else ""
    if image:
        return (f'<div class="thumb{" face" if face else ""}" data-ph="{E(ph)}"><img src="{E(image)}" alt="" '
                f'loading="lazy" decoding="async" referrerpolicy="no-referrer">{play}</div>')
    return f'<div class="thumb ph"><span>{E(ph)}</span>{play}</div>'


def item_html(key: str, packet: str, title: str, *, label: str = "", when: str = "",
              meta: str = "", summary: str = "", anchor: bool = True, caption: str = "", caption_label: str = "Caption",
              writable: bool = False, image: str = "", ph: str = "", face: bool = False, video: bool = False,
              terms: list[str] | None = None, slot: str = "", slot_iso: str = "", thumb: bool = True,
              mutable: bool = True) -> str:
    """One story. `caption` is ready caption HTML shown behind a button; `slot` is the schedule time."""
    idattr = f' id="s-{E(key)}"' if anchor else ""
    classes = "item" + ("" if thumb else " noimg")
    extra = f' data-slot="{E(slot_iso)}"' if slot_iso else ""
    if terms:
        extra += f' data-terms="{E("|".join(terms))}"'
    out = [f'<article class="{classes}"{idattr} data-id="{E(key)}" data-packet="{E(packet)}"{extra}>']
    if slot:
        out.append(slot)
    if thumb:
        out.append(thumb_html(image, ph or title[:40], face=face, video=video))
    k = kicker(label)
    kick = " ".join(x for x in (k, f'<span class="when">{E(when)}</span>' if when else "") if x)
    out.append('<div class="body">')
    if kick:
        out.append(f'<div class="kick">{kick}</div>')
    out.append(f'<span class="t">{E(title)}</span>')
    if summary:
        out.append(f'<div class="sum">{E(summary)}</div>')
    if meta:
        out.append(f'<div class="meta">{meta}</div>')
    out.append('</div>')  # caption tools sit under the picture and text, full width
    acts = []
    if caption:
        acts.append(f'<button type="button" class="act" data-toggle="cap">{ICONS["doc"]}{E(caption_label)}</button>')
    if writable:
        acts.append(f'<button type="button" class="act writecap-btn">{ICONS["spark"]}Write caption</button>')
    if mutable:
        acts.append(f'<button type="button" class="act quiet" data-toggle="mute">{ICONS["eyeoff"]}Not sitcom</button>')
    if acts:
        out.append(f'<div class="acts">{"".join(acts)}</div>')
    if caption:
        out.append(f'<div class="panel" data-panel="cap" hidden>{caption}</div>')
    if writable:
        out.append('<div class="writer-out panel"></div>')
    out.append('<input type="checkbox" class="pick" aria-label="Pick this story"></article>')
    return "\n".join(out)


def cluster_ph(c: dict) -> str:
    """Text for the picture tile when a story has no picture: the show, else the person."""
    if c["shows"]:
        return sorted(c["shows"], key=len)[0]
    if c["people"]:
        return sorted(c["people"])[0]
    return KICKERS.get(chip_for(c), ("",))[0] or c["primary"]["outlet"]


def cluster_html(c: dict, now: dt.datetime, when: str | None = None, *, slot: str = "", slot_iso: str = "",
                 anchor: bool = True) -> str:
    n = len(c["sources"])
    src = links_html(c["sources"])
    meta = src if c["kind"] == "video" else f'{n} outlet{"s" if n != 1 else ""}: {src}' if n > 1 else src
    return item_html(c["key"], story_packet(c), c["primary"]["title"], label=chip_for(c),
                     when=when if when is not None else ago(c["time"], now),
                     meta=meta, summary=c["summary"], caption=draft_caption_html(c["key"]),
                     caption_label="Draft caption", writable=c["kind"] == "article",
                     image=c.get("image", ""), ph=cluster_ph(c), video=c["kind"] == "video",
                     terms=sorted(c["shows"]) + sorted(c["people"]), slot=slot, slot_iso=slot_iso, anchor=anchor)


def cluster_list(clusters: list[dict], now: dt.datetime) -> str:
    if not clusters:
        return '<p class="empty">Nothing new here.</p>'
    shown = "\n".join(cluster_html(c, now) for c in clusters[:SECTION_CAP])
    rest = clusters[SECTION_CAP:]
    if rest:
        shown += (f'\n<details class="more"><summary class="act">Show {len(rest)} more</summary>\n'
                  + "\n".join(cluster_html(c, now) for c in rest) + "\n</details>")
    return shown


def anniv_total(ann: dict) -> int:
    return sum(len(ann.get(k, [])) for k in ("birthdays", "deaths", "premieres", "finales"))


def first_show(shows: list[str]) -> str:
    m = re.match(r"'(.+?)'", shows[0]) if shows else None
    return m.group(1) if m else ""


def anniv_html(ann: dict, today: dt.date) -> str:
    parts = []

    def links(e):
        return links_html(links_for(e))

    def block(title, rows, render):
        parts.append(f"<h3>{E(title)}</h3>")
        parts.append("\n".join(render(r) for r in rows) if rows else '<p class="empty">None today.</p>')

    # birthdays first, always
    block("Birthdays", ann.get("birthdays", []), lambda b: item_html(
        f"bday-{b['qid']}-{today.year}", birthday_packet(b),
        f"{b['name']}, turns {b['years']} today", label="Birthday", when=f"born {b['date'].year}",
        meta=links(b), summary=", ".join(b["shows"]), image=b.get("image", ""), ph=b["name"], face=True,
        mutable=False, caption=caption_html(birthday_caption(b), "Caption in your birthday format")))
    block("Death anniversaries", ann.get("deaths", []), lambda d: item_html(
        f"death-{d['qid']}-{today.year}", death_packet(d),
        f"{d['name']}, passed away {d['years']} years ago today", label="Remembering",
        when=str(d["date"].year), meta=links(d), image=d.get("image", ""), ph=d["name"], face=True, mutable=False,
        summary=(f"{short_date(d['born'])} - " if d.get("born") else "") + short_date(d["date"])
        + ". " + ", ".join(d["shows"]),
        caption=caption_html(death_caption(d), "Caption in your remembrance format")))
    block("Premieres", ann.get("premieres", []), lambda s: item_html(
        f"prem-{s['qid']}-{today.year}", show_anniv_packet(s, "premiered"),
        f"'{s['name']}' premiered {s['years']} years ago today", label="Anniversary",
        when=str(s["date"].year), meta=links(s), summary=s["networks"], image=s.get("image", ""), ph=s["name"],
        mutable=False, caption=caption_html(show_anniv_caption(s, "premiered"), "Caption in your anniversary format")))
    block("Finales", ann.get("finales", []), lambda s: item_html(
        f"fin-{s['qid']}-{today.year}", show_anniv_packet(s, "ended"),
        f"'{s['name']}' ended {s['years']} years ago today", label="Finale",
        when=str(s["date"].year), meta=links(s), summary=s["networks"], image=s.get("image", ""), ph=s["name"],
        mutable=False, caption=caption_html(show_anniv_caption(s, "ended"), "Caption in your anniversary format")))
    return "\n".join(parts)


def upcoming_html(ups: list[dict]) -> str:
    if not ups:
        return ""
    rows = []
    for u in ups:
        net = f" on {u['networks']}" if u["networks"] else ""
        meta = (f'<a href="https://www.themoviedb.org/tv/{u["id"]}" target="_blank" '
                f'rel="noopener">TMDB</a>, {E(u["label"])}')
        rows.append(item_html(f"tmdb-{u['id']}-{u['date'].isoformat()}", upcoming_packet(u),
                              f"'{u['name']}'{net}", label="Premiere",
                              when=f"{u['date'].strftime('%a')}, {u['date'].strftime('%b')} {u['date'].day}",
                              meta=meta, summary=u["overview"], image=u.get("image", ""), ph=u["name"],
                              mutable=False))
    return "<h3>Premiere dates from TMDB</h3>\n" + "\n".join(rows)


def health_html(health: list[dict]) -> str:
    bad = [h for h in health if not h["ok"]]
    rows = []
    for h in health:
        status = "ok" if h["ok"] else "FAILED"
        note = h.get("error") or (f"channel: {h['resolved']}" if h.get("resolved") and h["type"] == "youtube" else "")
        rows.append(f"<tr><td>{E(h['type'])}</td><td>{E(h['name'])}</td><td>{status}</td>"
                    f"<td>{h.get('count', 0)}</td><td>{E(note)}</td></tr>")
    summary = f"{len(health) - len(bad)} sources worked" + (f", {len(bad)} failed" if bad else "")
    return (f'<details class="health"><summary>{summary}. Show the list</summary>'
            '<div class="tablewrap"><table><tr><th>Type</th><th>Source</th><th>Status</th><th>Stories</th><th>Note</th></tr>'
            + "".join(rows) + "</table></div></details>")


def post_today_html(ranked: list[dict], now: dt.datetime) -> str:
    if not ranked:
        return ""
    start = (now + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    rows = []
    for i, c in enumerate(ranked):
        slot = start + dt.timedelta(hours=i)
        h = slot.hour % 12 or 12
        ampm = "am" if slot.hour < 12 else "pm"
        mins = ":%02d" % slot.minute if slot.minute else ""
        tm = (f'<div class="time" aria-label="Post around {clock(slot)} GMT">{h}<small>{mins}{ampm}</small></div>')
        rows.append(cluster_html(c, now, slot=tm, slot_iso=iso(slot), anchor=False))
    top = ('<div class="sched-top"><span><b id="sched-done">0</b> of '
           f'{len(ranked)} posted. Times are GMT.</span><span id="sched-next" class="next-when"></span></div>'
           '<div class="progress" aria-hidden="true"><i id="sched-bar"></i></div>')
    return section("today", "Today's posting schedule", None, top + '\n<div class="today">\n' + "\n".join(rows) + "\n</div>")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def write_assets() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS_DIR / "app.js", DOCS_DIR / "app.js")
    for name in ("icon-180.png", "icon-192.png", "icon-512.png", "logo-96.png", "logo-512.png", "og-image.jpg",
                 "favicon-16.png", "favicon-32.png", "favicon-48.png", "favicon.ico", "site.webmanifest"):
        shutil.copyfile(ASSETS_DIR / name, DOCS_DIR / name)
    (DOCS_DIR / "fonts").mkdir(exist_ok=True)
    shutil.copyfile(ASSETS_DIR / "fonts" / "archivo.woff2", DOCS_DIR / "fonts" / "archivo.woff2")
    shutil.copyfile(ASSETS_DIR / "fonts" / "OFL-Archivo.txt", DOCS_DIR / "fonts" / "OFL-Archivo.txt")
    write_text(DOCS_DIR / ".nojekyll", "")


def breaking_clusters(state: dict, now: dt.datetime) -> list[dict]:
    cutoff = now - dt.timedelta(hours=BREAKING_PAGE_HOURS)
    items = [it for it in state["items"].values()
             if it["kind"] == "article" and it.get("relevant") and it.get("strong")
             and any(t in BREAKING_TAGS for t in it.get("tags", []))
             and (parse_iso(it["first_seen"]) or now) >= cutoff and is_fresh(it, now, BREAKING_PAGE_HOURS)]
    clusters = [c for c in build_clusters(items) if c["breaking"]]
    clusters.sort(key=lambda c: c["first_seen"], reverse=True)
    return clusters


def write_breaking_page(clusters: list[dict], now: dt.datetime) -> None:
    feed = breaking_feed(clusters)
    write_text(DOCS_DIR / "breaking.json", json.dumps({"checked": iso(now), "stories": feed}, ensure_ascii=False))
    body = ['<div class="intro"><h1>Breaking</h1>',
            f"<p>Deaths, renewals, cancellations, exits and revivals, newest first. "
            f"Last checked {clock(now)} GMT. Checks run every 30 minutes.</p></div>"]
    if clusters:
        rows = []
        for c in clusters:
            seen = c["first_seen"]
            when = f"seen {clock(seen)}" + ("" if seen.date() == now.date() else f", {seen.strftime('%b')} {seen.day}")
            rows.append(cluster_html(c, now, when=when))
        inner = "\n".join(rows)
    else:
        inner = '<p class="empty">Nothing breaking right now. Alerts go to Telegram the moment something lands.</p>'
    body.append(section("breaking", f"Last {BREAKING_PAGE_HOURS} hours", len(clusters), inner))
    write_text(DOCS_DIR / "breaking.html",
               page("Breaking: Sitcom Crave Roundup", "\n".join(body), active="breaking", breaking=feed,
                    desc="Breaking comedy TV news: deaths, renewals, cancellations, exits and revivals "
                         f"from the last {BREAKING_PAGE_HOURS} hours."))


def write_captions_page() -> None:
    body = ('<div class="intro"><h1>Caption maker</h1>'
            "<p>Paste a link to any story, or the story's text, and Gemini writes the caption in your style, "
            "following your caption rules. It uses the Gemini key saved in Settings at the bottom of this page.</p></div>\n"
            + MAKER)
    write_text(DOCS_DIR / "captions.html", page("Caption maker: Sitcom Crave Roundup", body, active="captions",
                                                desc="Write a Sitcom Crave caption from any link or story text."))


def write_archive(state: dict) -> None:
    rows, day = [], None
    for ed in reversed(state.get("editions", [])):
        t = parse_iso(ed["time"])
        if not t:
            continue
        if t.date() != day:
            day = t.date()
            rows.append(f'<h3 class="archday">{E(long_date(day))}</h3>')
        rows.append(f'<a class="arch" href="editions/{E(ed["name"])}.html">'
                    f'<b>{E(ed["edition"].title())}</b><span>{clock(t)} GMT</span></a>')
    body = ('<div class="intro"><h1>Archive</h1><p>Every morning and evening roundup, newest first.</p></div>\n'
            + ("\n".join(rows) if rows else '<p class="empty">No roundups yet.</p>'))
    write_text(DOCS_DIR / "archive.html", page("Archive: Sitcom Crave Roundup", body, active="archive",
                                               desc="Every morning and evening Sitcom Crave roundup."))


def build_edition(mode: str, state: dict, health: list[dict], now: dt.datetime, cfg: dict | None = None) -> str:
    name = f"{now.date().isoformat()}-{mode}"
    # re-running today's edition rebuilds it from the same starting point
    earlier = [e for e in state.get("editions", []) if e["name"] != name]
    prev = parse_iso(earlier[-1]["time"]) if earlier else None
    start = prev or now - dt.timedelta(hours=24)
    def in_window(it):
        seen = parse_iso(it["first_seen"]) or now
        # stories already seen by the previous edition (same timestamp) are not repeated
        return (seen > start if prev else seen >= start) and is_fresh(it, now)
    items = [it for it in state["items"].values() if in_window(it)]
    relevant_items = [it for it in items if it.get("relevant")]
    other_videos = [it for it in items if it["kind"] == "video" and not it.get("relevant")]

    clusters = build_clusters(relevant_items)
    shown_before = [e for e in state.get("shown", []) if e.get("edition") != name
                    and (parse_iso(e.get("at")) or now) >= now - dt.timedelta(hours=REPEAT_HOURS)]
    before = len(clusters)
    clusters = [c for c in clusters if not already_shown(c, shown_before)]
    if before != len(clusters):
        n_rep = before - len(clusters)
        log(f"  left out {n_rep} {'story' if n_rep == 1 else 'stories'} already in an earlier roundup")
    for c in clusters:
        c["score"] = score(c, now)
    by_section: dict[str, list[dict]] = {k: [] for k, _ in SECTIONS}
    for c in sorted(clusters, key=lambda c: (-c["score"], -c["time"].timestamp())):
        by_section[c["section"]].append(c)

    ranked = [c for c in sorted(clusters, key=lambda c: -c["score"]) if c["kind"] == "article"][:10]

    today = now.date()
    if cfg is not None and os.environ.get("GEMINI_API_KEY"):
        log("Caption drafts")
        draft_many(ranked + by_section["breaking"], cfg)
    log("Pictures")
    order = ranked + by_section["breaking"] + [c for k, _ in SECTIONS for c in by_section[k]]
    seen_keys: set = set()
    fill_images([c for c in order if not (c["key"] in seen_keys or seen_keys.add(c["key"]))])
    log("Extras")
    ann = anniversaries(today, health)  # every edition, so today's birthdays stay on the front page all day
    for s in ann.get("premieres", []) + ann.get("finales", []):
        s["image"] = tmdb_show_image(s["name"])
    for b in ann.get("birthdays", []) + ann.get("deaths", []):
        if not b.get("image"):
            b["image"] = tmdb_person_image(b["name"])
    ups = upcoming(today, health)
    live_breaking = breaking_feed(breaking_clusters(state, now))

    title = f"{mode.title()} roundup, {long_date(today)}"
    if prev:
        since = f"since {clock(start)} GMT" + (" yesterday" if start.date() < today else "")
    else:
        since = "from the last 24 hours"
    nxt = "The evening roundup lands around 6pm." if mode == "morning" else "The morning roundup lands around 7am."
    body = [f'<div class="intro"><h1>{E(mode.title())} roundup</h1>'
            f"<p>{E(long_date(today))}. Updated {clock(now)} GMT with {len(clusters)} stories {E(since)}. "
            f"{nxt}</p></div>",
            post_today_html(ranked, now)]
    chips = [("today", "Schedule", None)] if ranked else []

    for key, label in SECTIONS:
        group = by_section[key]
        if key == "news":  # On this day sits near the top: it only counts today
            body.append(section("anniversaries", f"On this day, {today.strftime('%B')} {today.day}",
                                anniv_total(ann), anniv_html(ann, today)))
            chips.append(("anniversaries", "On this day", anniv_total(ann)))
        if key == "breaking" and not group:
            continue
        n = len(group) + (len(ups) if key == "upcoming" else 0)
        inner = []
        if key in ("interviews", "trailers"):
            arts = [c for c in group if c["kind"] == "article"]
            vids = [c for c in group if c["kind"] == "video"]
            inner.append(cluster_list(arts, now) if arts else "")
            if vids:
                inner.append("<h3>" + ("Talk-show and podcast clips" if key == "interviews" else "Trailer and promo videos") + "</h3>")
                inner.append(cluster_list(vids, now))
            if not arts and not vids:
                inner.append('<p class="empty">Quiet.</p>')
            kinds = ("talk", "outlet") if key == "interviews" else ("streamer",)
            others = [v for v in other_videos if v.get("channel_kind") in kinds]
            if others:
                others.sort(key=lambda v: item_time(v), reverse=True)
                oc = build_clusters(others)
                inner.append(f'<details class="other"><summary class="act">Other uploads, not on the watchlist ({len(oc)})</summary>'
                             + "\n".join(cluster_html(c, now) for c in oc) + "</details>")
        elif key == "upcoming":  # premiere dates first, then news about shows on the way
            inner.append(upcoming_html(ups))
            if group and ups:
                inner.append("<h3>Coming-up news</h3>")
            inner.append(cluster_list(group, now) if group or not ups else "")
        else:
            inner.append(cluster_list(group, now))
        body.append(section(key, label, n, "\n".join(x for x in inner if x)))
        if n or key == "breaking":
            chips.append((key, JUMP_LABELS[key], n))

    body.append(section("sources", "Sources", None, health_html(health)))

    content = "\n".join(b for b in body if b)
    lead = f" Top story: {ranked[0]['primary']['title']}" if ranked else ""
    desc = f"{mode.title()} roundup for {long_date(today)}: {len(clusters)} comedy TV {"story" if len(clusters) == 1 else "stories"}.{lead}"
    write_text(DOCS_DIR / "editions" / f"{name}.html",
               page(f"{title}: Sitcom Crave Roundup", content, depth=1, chips=chips, desc=desc,
                    breaking=live_breaking, banner=True))
    write_text(DOCS_DIR / "index.html",
               page("Sitcom Crave Roundup", content, depth=0, chips=chips, desc=desc,
                    breaking=live_breaking, banner=True))
    rerun = any(e["name"] == name for e in state.get("editions", []))
    url = site_url()
    top = "\n".join(f"{i + 1}. {c['primary']['title']}" for i, c in enumerate(ranked[:3]))
    if not rerun:  # one Telegram note per edition, even if it's rebuilt
        state["_notice"] = (f"{mode.title()} roundup is ready: {len(clusters)} stories.\n\nTop picks:\n{top}"
                            + (f"\n\n{url}/" if url else ""))
    state["editions"] = [e for e in state.get("editions", []) if e["name"] != name]
    state["editions"].append({"name": name, "edition": mode, "time": iso(now)})
    keep = [e for e in state.get("shown", []) if e.get("edition") != name
            and (parse_iso(e.get("at")) or now) >= now - dt.timedelta(hours=REPEAT_HOURS)]
    keep += [{"edition": name, "at": iso(now), "tokens": sorted(c["tokens"]), "shows": sorted(c["shows"]),
              "people": sorted(c["people"]), "tags": sorted(c["tags"]), "ids": [m["id"] for m in c["members"]]}
             for c in clusters if c["kind"] == "article"]
    state["shown"] = keep
    return name


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

MORNING_AT = (6, 30)   # GMT: from here on, a run makes the morning roundup if today's is missing
EVENING_AT = (17, 30)  # GMT: same for the evening roundup


def pick_mode(state: dict, now: dt.datetime) -> str:
    """For 'auto' runs: catch up on a missed roundup, otherwise do a breaking check.
    GitHub's scheduler can run late or skip runs, so any run that does happen can fill the gap."""
    done = {e["name"] for e in state.get("editions", [])}
    today = now.date().isoformat()
    minutes = now.hour * 60 + now.minute
    if minutes >= EVENING_AT[0] * 60 + EVENING_AT[1] and f"{today}-evening" not in done:
        return "evening"
    if minutes >= MORNING_AT[0] * 60 + MORNING_AT[1] and minutes < EVENING_AT[0] * 60 + EVENING_AT[1] \
            and f"{today}-morning" not in done:
        return "morning"
    return "breaking"


def run(mode: str) -> int:
    global USER_AGENT
    now = now_utc()
    for name in ("TMDB_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY"):
        raw = os.environ.get(name, "")
        parts = raw.split()
        if len(parts) > 1:
            log(f"  Note: the {name} secret holds {len(parts)} values; using the first one")
        os.environ[name] = parts[0] if parts else ""  # pasted secrets often carry stray spaces or extra lines
        _SECRETS.extend(parts)
    _SECRETS.extend([os.environ.get("TMDB_API_KEY", ""), os.environ.get("TELEGRAM_BOT_TOKEN", "")])
    cfg = json.loads((CONFIG_DIR / "sources.json").read_text(encoding="utf-8"))
    USER_AGENT = cfg.get("user_agent", USER_AGENT)
    wl = Watchlist(load_lines(CONFIG_DIR / "watchlist_shows.txt"),
                   load_lines(CONFIG_DIR / "watchlist_people.txt"))
    first_run = not STATE_FILE.exists()
    _SECRETS.append(os.environ.get("GEMINI_API_KEY", ""))
    global FRESH_HOURS, BREAKING_ALERT_HOURS, REPEAT_HOURS, NOT_NEWS
    fr = cfg.get("freshness", {})
    FRESH_HOURS = float(fr.get("max_age_hours", FRESH_HOURS))
    BREAKING_ALERT_HOURS = float(fr.get("breaking_alert_max_age_hours", BREAKING_ALERT_HOURS))
    REPEAT_HOURS = float(fr.get("repeat_memory_hours", REPEAT_HOURS))
    NOT_NEWS = load_not_news()
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        telegram_chat(cfg)
    state = load_state()
    if mode == "auto":
        mode = pick_mode(state, now)
        log(f"auto: doing the {mode} run")
    try:  # lets the workflow name its commit after what actually ran
        (Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "roundup_mode").write_text(mode)
    except OSError:
        pass
    global DRAFTS, IMAGES
    DRAFTS = state.setdefault("drafts", {})
    refilter(state, wl, cfg)
    log(f"{mode} run at {iso(now)}")

    raw, health = collect(mode, cfg, wl)
    added = merge(state, raw, wl, now)
    prune(state, now)
    DRAFTS = state["drafts"]  # prune made a fresh dict; keep writing into the one that gets saved
    IMAGES = state["images"]
    log(f"{len(raw)} stories fetched, {added} new comedy stories, {len(state['items'])} remembered")

    # breaking: alert once per story, keep the breaking page current
    clusters = breaking_clusters(state, now)
    alerted = state.setdefault("alerted", {})
    alert_cutoff = now - dt.timedelta(hours=BREAKING_ALERT_HOURS)
    fresh = [c for c in clusters if not any(m["id"] in alerted for m in c["members"])
             and c["time"] >= alert_cutoff]  # older stories still show on the page, but don't buzz your phone
    sent = 0
    if first_run:
        log("First run: noting current breaking stories without alerting")
    else:
        draft_many(fresh[:MAX_ALERTS], cfg, limit=MAX_ALERTS)
        for c in fresh[:MAX_ALERTS]:
            if send_telegram(alert_text(c), cfg):
                sent += 1
        if len(fresh) > MAX_ALERTS:
            url = site_url()
            send_telegram(f"{len(fresh) - MAX_ALERTS} more breaking stories"
                          + (f": {url}/breaking.html" if url else " on the breaking page"))
    for c in fresh:
        for m in c["members"]:
            alerted[m["id"]] = iso(now)
    log(f"{len(fresh)} new breaking stories, {sent} Telegram alerts sent")

    write_assets()
    write_captions_page()
    if fresh or mode != "breaking" or not (DOCS_DIR / "breaking.html").exists():
        fill_images(clusters, limit=15)
        write_breaking_page(clusters, now)

    if mode == "breaking":
        if fresh:
            save_state(state)
        else:
            log("Nothing new and breaking; leaving files untouched")
        return 0

    name = build_edition(mode, state, health, now, cfg)
    notice = state.get("_notice")
    if notice:
        send_telegram(notice, cfg)
    write_archive(state)
    write_text(DATA_DIR / "health.json", json.dumps(health, indent=1))
    state.pop("_notice", None)
    save_state(state)
    log(f"Wrote edition {name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["morning", "evening", "breaking", "auto"])
    args = ap.parse_args()
    return run(args.mode)


if __name__ == "__main__":
    sys.exit(main())
