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
FRESH_HOURS = 36       # stories older than this are never shown as new
BREAKING_PAGE_HOURS = 48
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

DEFAULT_HEADER = (
    "Data processing and Captioner: caption these for X in my style. "
    "Post only the finished pieces."
)

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

_SECRETS: list[str] = []


def redact(text: str) -> str:
    """Hide API keys and tokens from anything that gets logged or saved."""
    for s in _SECRETS:
        if s:
            text = text.replace(s, "***")
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
    raise FetchError(redact(f"{type(last).__name__}: {last}") if last else "request failed")


# --------------------------------------------------------------------------
# Matching and tagging
# --------------------------------------------------------------------------

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


def item_time(it: dict) -> dt.datetime:
    return parse_iso(it.get("published")) or parse_iso(it.get("first_seen")) or dt.datetime.min.replace(tzinfo=UTC)


def is_fresh(it: dict, now: dt.datetime) -> bool:
    pub = parse_iso(it.get("published"))
    return pub is None or pub >= now - dt.timedelta(hours=FRESH_HOURS)


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
    p = c["primary"]
    if c["breaking"]:
        typ = f"Breaking ({', '.join(c['breaking'])})"
    elif c["kind"] == "video":
        typ = "Trailer" if c["section"] == "trailers" else "Interview clip"
    else:
        typ = SECTION_TYPE[c["section"]]
    lines = [f"Type: {typ}", f"Headline: {p['title']}"]
    if c["summary"]:
        lines.append(f"Details: {c['summary']}")
    if c["kind"] == "video":
        lines.append(f"Video: {p['outlet']} on YouTube, {p['link']}")
    if c["shows"]:
        lines.append("Shows: " + ", ".join(f"'{s}'" for s in sorted(c["shows"])))
    if c["people"]:
        lines.append("People: " + ", ".join(sorted(c["people"])))
    lines.append("Sources: " + md_sources(c["sources"]))
    return "\n".join(lines)


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
SELECT ?person ?name ?date ?born ?links ?gender ?article
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
    lines = ["Type: Birthday", f"Name: {b['name']}, turns {b['years']} today"]
    if b["gender"]:
        lines.append(f"Gender: {b['gender']}")
    lines.append(f"Born: {us_date(b['date'])}")
    if b["shows"]:
        lines.append("Known for: " + ", ".join(b["shows"]))
    lines.append("Sources: " + md_sources(links_for(b)))
    return "\n".join(lines)


def death_packet(d: dict) -> str:
    lines = ["Type: Death anniversary",
             f"Name: {d['name']}, passed away {d['years']} years ago today"]
    if d["gender"]:
        lines.append(f"Gender: {d['gender']}")
    born = short_date(d["born"]) if d.get("born") else "?"
    lines.append(f"Dates: {born} - {short_date(d['date'])}")
    if d["shows"]:
        lines.append("Known for: " + ", ".join(d["shows"]))
    lines.append("Sources: " + md_sources(links_for(d)))
    return "\n".join(lines)


def show_anniv_packet(s: dict, verb: str) -> str:
    kind = "Premiere anniversary" if verb == "premiered" else "Finale anniversary"
    net = f", {s['networks']}" if s["networks"] else ""
    return "\n".join([
        f"Type: {kind}",
        f"Show: '{s['name']}' {verb} {s['years']} years ago today",
        f"Date: {us_date(s['date'])}{net}",
        "Sources: " + md_sources(links_for(s)),
    ])


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
            nets = ", ".join(n.get("name", "") for n in d.get("networks", [])[:2])
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
                        "overview": trim(d.get("overview") or "", 260)}
        res = sorted(out.values(), key=lambda x: x["date"])
        health.append({"type": "tmdb", "name": "upcoming premieres", "ok": True, "count": len(res)})
        log(f"  ok   tmdb upcoming: {len(res)}")
        return res
    except Exception as e:
        msg = redact(f"{type(e).__name__}: {e}")[:200]
        health.append({"type": "tmdb", "name": "upcoming premieres", "ok": False, "count": 0, "error": msg})
        log(f"  FAIL tmdb upcoming: {msg}")
        return sorted(out.values(), key=lambda x: x["date"])


def upcoming_packet(u: dict) -> str:
    net = f" on {u['networks']}" if u["networks"] else ""
    lines = ["Type: Upcoming premiere", f"Show: '{u['name']}' ({u['label']})",
             f"Premieres: {long_date(u['date'])}{net}"]
    if u["overview"]:
        lines.append(f"About: {u['overview']}")
    lines.append(f"Sources: [TMDB](https://www.themoviedb.org/tv/{u['id']})")
    return "\n".join(lines)


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


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
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
    lines = [f"BREAKING ({', '.join(c['breaking'])}): {p['title']}"]
    for outlet, link in c["sources"][:3]:
        lines.append(f"{outlet}: {link}")
    url = site_url()
    if url:
        lines.append(f"Roundup: {url}/breaking.html")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

E = html.escape

CSS = """
:root{--navy:#081823;--navy2:#13222d;--accent:#ff751f;--pink:#ff5fa8;--bg:#f3f4f6;--card:#fff;--fg:#14181c;--muted:#6b7280;--line:#e5e7eb;--link:#1f5fbf;--sel:#fff4ec;--sum:#3b4148;--btn:#eef0f3;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#0b1117;--card:#151d25;--fg:#e9edf1;--muted:#8d98a5;--line:#243040;--link:#8ab4ff;--sel:#2a1f17;--sum:#c3cbd4;--btn:#1e2833;--shadow:none}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,system-ui,"Segoe UI",Roboto,sans-serif;font-size:16px;line-height:1.45;color:var(--fg);background:var(--bg)}
body.has-sel main{padding-bottom:calc(150px + env(safe-area-inset-bottom))}
a{color:var(--link);text-decoration:none}
main{max-width:760px;margin:0 auto;padding:0 max(12px,env(safe-area-inset-right)) calc(40px + env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left))}
header.top{position:sticky;top:0;z-index:5}
.bar{background:var(--navy);color:#fff;padding:calc(8px + env(safe-area-inset-top)) max(14px,env(safe-area-inset-right)) 0 max(14px,env(safe-area-inset-left))}
.bar .in{max-width:760px;margin:0 auto}
.brand{display:inline-flex;align-items:baseline;gap:7px;color:#fff;font-weight:900;letter-spacing:.04em;font-size:17px;padding:2px 0 6px}
.brand .b2{color:var(--accent)}
.tabs{display:flex;gap:4px}
.tabs a{color:#c9d3dc;font-weight:700;font-size:15px;padding:9px 12px 10px;border-bottom:3px solid transparent}
.tabs a.on{color:#fff;border-bottom-color:var(--accent)}
.jumpwrap{background:var(--bg);border-bottom:1px solid var(--line)}
nav.jump{max-width:760px;margin:0 auto;display:flex;gap:7px;overflow-x:auto;white-space:nowrap;-webkit-overflow-scrolling:touch;scrollbar-width:none;padding:9px max(12px,env(safe-area-inset-right)) 9px max(12px,env(safe-area-inset-left))}
nav.jump::-webkit-scrollbar{display:none}
nav.jump a{flex:0 0 auto;font-size:14px;font-weight:600;padding:6px 12px;border-radius:999px;background:var(--card);color:var(--fg);border:1px solid var(--line)}
nav.jump a .n{color:var(--muted);font-weight:600;margin-left:4px}
.intro{padding:16px 2px 4px}
.intro h1{font-size:24px;line-height:1.2;font-weight:800;margin:0}
.intro p{margin:4px 0 0;color:var(--muted);font-size:14px}
h2{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:26px 2px 10px;scroll-margin-top:120px}
h2 .count{background:var(--btn);color:var(--muted);border-radius:999px;padding:1px 8px;font-size:12px;letter-spacing:0}
h3{font-size:14px;font-weight:700;color:var(--muted);margin:18px 2px 8px}
.muted{color:var(--muted);font-size:14px}
.item{position:relative;background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:12px 14px 12px;margin:0 0 10px;scroll-margin-top:120px;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:border-color .15s,background .15s}
.item.sel{border-color:var(--accent);background:var(--sel);box-shadow:0 0 0 1px var(--accent)}
.item .top{display:flex;align-items:center;gap:8px;margin-bottom:6px;min-height:22px}
.item .when{color:var(--muted);font-size:13px;margin-left:auto;white-space:nowrap}
.item label{display:flex;gap:12px;align-items:flex-start;cursor:pointer}
.item .t{font-weight:700;font-size:16.5px;line-height:1.3}
.item .sum{color:var(--sum);font-size:14.5px;margin:6px 36px 0 0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.item .meta{color:var(--muted);font-size:13px;margin-top:8px;line-height:1.6}
.item .meta details{display:inline}
.item .meta summary{display:inline;color:var(--link);cursor:pointer;list-style:none}
.item .meta summary::-webkit-details-marker{display:none}
input.pick{-webkit-appearance:none;appearance:none;order:2;margin:0 0 0 auto;flex:0 0 26px;width:26px;height:26px;border:2px solid #c3c9d1;border-radius:50%;background:var(--card);cursor:pointer}
input.pick:checked{background:var(--accent) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M5 12.5l4.5 4.5L19 7.5' fill='none' stroke='white' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/16px no-repeat;border-color:var(--accent)}
.chip{display:inline-block;font-size:11.5px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;padding:3px 8px;border-radius:6px;color:#fff;background:#5b6573;white-space:nowrap}
.c-death,.c-remember{background:#2b2f36}
.c-renewal{background:#15803d}
.c-cancel{background:#c62828}
.c-exit{background:#d9480f}
.c-arrest{background:#a61e4d}
.c-revival,.c-trailer{background:#7c3aed}
.c-casting{background:#1d4ed8}
.c-news{background:var(--navy2)}
.c-streaming{background:#4338ca}
.c-interview,.c-clip{background:#0f766e}
.c-upcoming,.c-premiere{background:#0369a1}
.c-birthday{background:var(--pink)}
.c-anniv{background:#b45309}
.item.rank .num{display:inline-flex;align-items:center;justify-content:center;min-width:26px;height:26px;border-radius:8px;background:var(--navy);color:#fff;font-weight:800;font-size:14px}
.item.rank .slot{font-weight:700;color:var(--accent);font-size:13px;margin-left:auto}
.item.posted{opacity:.5}
body.hideposted .item.posted{display:none}
.empty{color:var(--muted);font-size:14px;padding:4px 2px}
details.more > summary,details.other > summary{cursor:pointer;color:var(--link);font-weight:600;font-size:14px;padding:6px 2px;list-style:none}
details.more > summary::-webkit-details-marker,details.other > summary::-webkit-details-marker{display:none}
#settings{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:4px 14px;margin:26px 0 10px}
#settings > summary{cursor:pointer;font-weight:700;padding:10px 0;color:var(--muted)}
#settings label{display:flex;gap:10px;align-items:center;padding:8px 0;font-size:15px}
#settings input[type=checkbox]{width:22px;height:22px;accent-color:var(--accent)}
textarea{width:100%;font:inherit;font-size:16px;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg)}
#tray{position:fixed;left:0;right:0;bottom:0;z-index:10;background:var(--card);border-top:1px solid var(--line);box-shadow:0 -6px 20px rgba(0,0,0,.14);border-radius:18px 18px 0 0;padding:10px max(14px,env(safe-area-inset-right)) calc(10px + env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left));transform:translateY(115%);transition:transform .22s ease}
body.has-sel #tray{transform:none}
#tray .in{max-width:760px;margin:0 auto}
#tray .info{display:flex;align-items:center;gap:10px;font-size:15px;margin-bottom:9px;min-height:22px}
#tray .info b{white-space:nowrap}
#status{color:#15803d;font-size:13.5px;flex:1;line-height:1.3}
#tray .linkbtn{background:none;border:0;color:var(--link);font-size:15px;font-weight:600;padding:4px}
#tray .btns{display:grid;grid-template-columns:repeat(auto-fit,minmax(70px,1fr));gap:8px}
#tray .btns button{min-height:48px;font-size:15px;font-weight:700;border-radius:12px;border:0;background:var(--btn);color:var(--fg);padding:4px 6px;-webkit-tap-highlight-color:transparent}
#tray .btns button.primary{background:var(--accent);color:#fff}
#tray .btns button[hidden]{display:none}
.health{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:4px 14px}
.health > summary{cursor:pointer;padding:10px 0;font-weight:600;color:var(--muted);font-size:14px}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:10px}
table{border-collapse:collapse;font-size:12.5px}
td,th{border-bottom:1px solid var(--line);padding:5px 8px 5px 0;text-align:left;vertical-align:top}
a.card{display:block;color:var(--fg)}
"""

TRAY = """
<div id="tray" aria-live="polite"><div class="in">
<div class="info"><b><span id="count">0</span> selected</b><span id="status"></span><button id="clear" class="linkbtn">Clear</button></div>
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
<label><input type="checkbox" id="showposted"> Show stories I already posted</label>
<div class="muted" style="margin-top:6px">Grok header line (top of every packet, saved on this device)</div>
<textarea id="header" rows="3"></textarea>
<p class="muted">Tap any story to pick it. Share opens the iPhone share sheet so you can send the packet straight to the Grok app. Open Grok opens grok.com with it typed in. Both also copy it.</p>
</details>
"""

# label shown on each card, and its colour class
CHIP_CLASS = {
    "Death": "death", "Cancel/End": "cancel", "Renewal": "renewal", "Exit": "exit",
    "Arrest": "arrest", "Revival": "revival", "Casting": "casting", "News": "news",
    "Streaming": "streaming", "Interview": "interview", "Trailer": "trailer",
    "Upcoming": "upcoming", "Clip": "clip", "Birthday": "birthday", "Remembering": "remember",
    "Premiere": "premiere", "Anniversary": "anniv", "Finale": "anniv",
}


def chip(label: str) -> str:
    if not label:
        return ""
    cls = CHIP_CLASS.get(label, "news")
    text = "Cancelled/Ending" if label == "Cancel/End" else label
    return f'<span class="chip c-{cls}">{E(text)}</span>'


def chip_for(c: dict) -> str:
    if c["breaking"]:
        return c["breaking"][0]
    if c["kind"] == "video":
        return "Trailer" if c["section"] == "trailers" else "Clip"
    if "Casting" in c["tags"]:
        return "Casting"
    return SECTION_TYPE[c["section"]]


def page(title: str, body: str, depth: int = 0, jump: list[tuple[str, str, int | None]] | None = None,
         active: str = "latest") -> str:
    pre = "../" * depth
    on = ' class="on"'
    tabs = "".join(
        f'<a href="{pre}{href}"{on if key == active else ""}>{label}</a>'
        for key, label, href in (("latest", "Latest", "index.html"), ("breaking", "Breaking", "breaking.html"),
                                 ("archive", "Archive", "archive.html")))
    head = (f'<div class="bar"><div class="in"><a class="brand" href="{pre}index.html">'
            f'<span>SITCOM CRAVE</span><span class="b2">ROUNDUP</span></a>'
            f'<nav class="tabs">{tabs}</nav></div></div>')
    if jump:
        pills = "".join(f'<a href="#{E(i)}">{E(l)}' + (f'<span class="n">{n}</span>' if n else "") + "</a>"
                        for i, l, n in jump)
        head += f'<div class="jumpwrap"><nav class="jump">{pills}</nav></div>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="format-detection" content="telephone=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Roundup">
<meta name="theme-color" content="#081823">
<link rel="apple-touch-icon" href="{pre}icon-180.png">
<link rel="icon" type="image/png" href="{pre}icon-180.png">
<title>{E(title)}</title>
<style>{CSS}</style>
<script>window.ROUNDUP_DEFAULT_HEADER = {json.dumps(DEFAULT_HEADER)};</script>
</head><body class="hideposted">
<header class="top">{head}</header>
<main>
{body}
{SETTINGS}
</main>
{TRAY}
<script src="{pre}app.js"></script>
</body></html>
"""


def h2(label: str, anchor: str, count: int | None = None) -> str:
    n = f'<span class="count">{count}</span>' if count else ""
    return f'<h2 id="{E(anchor)}"><span>{E(label)}</span>{n}</h2>'


def links_html(sources: list[tuple[str, str]], show: int = 3) -> str:
    def a(o, u):
        return f'<a href="{E(u)}" target="_blank" rel="noopener">{E(o)}</a>'
    first = " · ".join(a(o, u) for o, u in sources[:show])
    rest = sources[show:]
    if not rest:
        return first
    return (f"{first} <details><summary>+{len(rest)} more</summary> · "
            + " · ".join(a(o, u) for o, u in rest) + "</details>")


def item_html(key: str, packet: str, title: str, *, label: str = "", when: str = "",
              meta: str = "", summary: str = "", anchor: bool = True, number: int | None = None,
              slot: str = "") -> str:
    idattr = f' id="s-{E(key)}"' if anchor else ""
    cls = "item rank" if number else "item"
    top = []
    if number:
        top.append(f'<span class="num">{number}</span>')
    top.append(chip(label))
    if slot:
        top.append(f'<span class="slot">{E(slot)}</span>')
    elif when:
        top.append(f'<span class="when">{E(when)}</span>')
    out = [f'<div class="{cls}"{idattr} data-id="{E(key)}" data-packet="{E(packet)}">',
           f'<div class="top">{"".join(top)}</div>',
           f'<label><span class="t">{E(title)}</span><input type="checkbox" class="pick" aria-label="Pick this story"></label>']
    if summary:
        out.append(f'<div class="sum">{E(summary)}</div>')
    if meta:
        out.append(f'<div class="meta">{meta}</div>')
    out.append("</div>")
    return "\n".join(out)


def cluster_html(c: dict, now: dt.datetime, when: str | None = None) -> str:
    return item_html(c["key"], story_packet(c), c["primary"]["title"], label=chip_for(c),
                     when=when if when is not None else ago(c["time"], now),
                     meta=links_html(c["sources"]), summary=c["summary"])


def cluster_list(clusters: list[dict], now: dt.datetime) -> str:
    if not clusters:
        return '<p class="empty">Quiet.</p>'
    shown = "\n".join(cluster_html(c, now) for c in clusters[:SECTION_CAP])
    rest = clusters[SECTION_CAP:]
    if rest:
        shown += (f'\n<details class="more"><summary>Show {len(rest)} more</summary>\n'
                  + "\n".join(cluster_html(c, now) for c in rest) + "\n</details>")
    return shown


def anniv_html(ann: dict, today: dt.date) -> str:
    total = sum(len(ann.get(k, [])) for k in ("birthdays", "deaths", "premieres", "finales"))
    parts = [h2(f"Anniversaries, {us_date(today)}", "sec-anniversaries", total)]

    def links(e):
        return " · ".join(f'<a href="{E(u)}" target="_blank" rel="noopener">{E(o)}</a>'
                          for o, u in links_for(e))

    def block(title, rows, render):
        parts.append(f"<h3>{E(title)}</h3>")
        parts.append("\n".join(render(r) for r in rows) if rows else '<p class="empty">None found.</p>')

    block("Birthdays", ann.get("birthdays", []), lambda b: item_html(
        f"bday-{b['qid']}-{today.year}", birthday_packet(b),
        f"{b['name']}, turns {b['years']} today", label="Birthday", when=f"Born {b['date'].year}",
        meta=links(b), summary=", ".join(b["shows"])))
    block("Death anniversaries", ann.get("deaths", []), lambda d: item_html(
        f"death-{d['qid']}-{today.year}", death_packet(d),
        f"{d['name']}, passed away {d['years']} years ago today", label="Remembering",
        when=str(d["date"].year), meta=links(d),
        summary=(f"{short_date(d['born'])} - " if d.get("born") else "") + short_date(d["date"])
        + ". " + ", ".join(d["shows"])))
    block("Premieres", ann.get("premieres", []), lambda s: item_html(
        f"prem-{s['qid']}-{today.year}", show_anniv_packet(s, "premiered"),
        f"'{s['name']}' premiered {s['years']} years ago today", label="Anniversary",
        when=str(s["date"].year), meta=links(s), summary=s["networks"]))
    block("Finales", ann.get("finales", []), lambda s: item_html(
        f"fin-{s['qid']}-{today.year}", show_anniv_packet(s, "ended"),
        f"'{s['name']}' ended {s['years']} years ago today", label="Finale",
        when=str(s["date"].year), meta=links(s), summary=s["networks"]))
    return "\n".join(parts)


def upcoming_html(ups: list[dict]) -> str:
    if not ups:
        return ""
    rows = []
    for u in ups:
        net = f" on {u['networks']}" if u["networks"] else ""
        meta = (f'<a href="https://www.themoviedb.org/tv/{u["id"]}" target="_blank" '
                f'rel="noopener">TMDB</a> · {E(u["label"])}')
        rows.append(item_html(f"tmdb-{u['id']}-{u['date'].isoformat()}", upcoming_packet(u),
                              f"'{u['name']}', {us_date(u['date'])}{net}", label="Premiere",
                              when=short_date(u["date"]).rsplit(",", 1)[0], meta=meta,
                              summary=u["overview"]))
    return "<h3>Premiere dates (TMDB)</h3>\n" + "\n".join(rows)


def health_html(health: list[dict]) -> str:
    bad = [h for h in health if not h["ok"]]
    rows = []
    for h in health:
        status = "ok" if h["ok"] else "FAILED"
        note = h.get("error") or (f"channel: {h['resolved']}" if h.get("resolved") and h["type"] == "youtube" else "")
        rows.append(f"<tr><td>{E(h['type'])}</td><td>{E(h['name'])}</td><td>{status}</td>"
                    f"<td>{h.get('count', 0)}</td><td>{E(note)}</td></tr>")
    return (f'<details class="health"><summary>Source health: {len(health) - len(bad)} ok, {len(bad)} failed</summary>'
            '<div class="tablewrap"><table><tr><th>Type</th><th>Source</th><th>Status</th><th>Stories</th><th>Note</th></tr>'
            + "".join(rows) + "</table></div></details>")


def post_today_html(ranked: list[dict], now: dt.datetime) -> str:
    if not ranked:
        return ""
    start = (now + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    rows = []
    for i, c in enumerate(ranked):
        slot = start + dt.timedelta(hours=i)
        n = len(c["sources"])
        meta = (f'{n} outlet{"s" if n != 1 else ""} · {ago(c["time"], now)} · '
                f'<a href="#s-{E(c["key"])}">details</a>')
        rows.append(item_html(c["key"], story_packet(c), c["primary"]["title"], label=chip_for(c),
                              meta=meta, anchor=False, number=i + 1, slot=f"~{clock(slot)}"))
    return (h2("Post today (ranked, times GMT)", "sec-today", len(ranked))
            + '\n<div class="today">\n' + "\n".join(rows)
            + '\n</div>\n<p class="muted" id="droppedline"></p>')


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def write_assets() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS_DIR / "app.js", DOCS_DIR / "app.js")
    shutil.copyfile(ASSETS_DIR / "icon-180.png", DOCS_DIR / "icon-180.png")
    write_text(DOCS_DIR / ".nojekyll", "")


def breaking_clusters(state: dict, now: dt.datetime) -> list[dict]:
    cutoff = now - dt.timedelta(hours=BREAKING_PAGE_HOURS)
    items = [it for it in state["items"].values()
             if it["kind"] == "article" and it.get("relevant") and it.get("strong")
             and any(t in BREAKING_TAGS for t in it.get("tags", []))
             and (parse_iso(it["first_seen"]) or now) >= cutoff and is_fresh(it, now)]
    clusters = [c for c in build_clusters(items) if c["breaking"]]
    clusters.sort(key=lambda c: c["first_seen"], reverse=True)
    return clusters


def write_breaking_page(clusters: list[dict], now: dt.datetime) -> None:
    body = ['<div class="intro"><h1>Breaking</h1>',
            f'<p>Last {BREAKING_PAGE_HOURS} hours · checked {clock(now)} GMT, {E(short_date(now.date()))} · '
            "updates about every 30 minutes</p></div>",
            h2("Breaking stories", "sec-breaking", len(clusters))]
    if clusters:
        for c in clusters:
            seen = c["first_seen"]
            when = f"Seen {clock(seen)}" + ("" if seen.date() == now.date() else f", {seen.strftime('%b')} {seen.day}")
            body.append(cluster_html(c, now, when=when))
    else:
        body.append('<p class="empty">Nothing breaking right now.</p>')
    write_text(DOCS_DIR / "breaking.html",
               page("Breaking: Sitcom Crave Roundup", "\n".join(body), active="breaking"))


def write_archive(state: dict) -> None:
    rows = []
    for ed in reversed(state.get("editions", [])):
        t = parse_iso(ed["time"])
        label = f"{ed['edition'].title()} roundup" if t else ed["name"]
        sub = f"{long_date(t.date())} · {clock(t)} GMT" if t else ""
        rows.append(f'<a class="item card" href="editions/{E(ed["name"])}.html">'
                    f'<div class="t">{E(label)}</div><div class="muted">{E(sub)}</div></a>')
    body = ('<div class="intro"><h1>Archive</h1><p>Every morning and evening roundup.</p></div>\n'
            + ("\n".join(rows) if rows else '<p class="empty">No roundups yet.</p>'))
    write_text(DOCS_DIR / "archive.html", page("Archive: Sitcom Crave Roundup", body, active="archive"))


def build_edition(mode: str, state: dict, health: list[dict], now: dt.datetime) -> str:
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
    for c in clusters:
        c["score"] = score(c, now)
    by_section: dict[str, list[dict]] = {k: [] for k, _ in SECTIONS}
    for c in sorted(clusters, key=lambda c: (-c["score"], -c["time"].timestamp())):
        by_section[c["section"]].append(c)

    ranked = [c for c in sorted(clusters, key=lambda c: -c["score"]) if c["kind"] == "article"][:10]

    today = now.date()
    log("Extras")
    ann = anniversaries(today, health) if mode == "morning" else {}
    ups = upcoming(today, health)

    title = f"{mode.title()} roundup, {long_date(today)}"
    since = f"since {clock(start)} GMT" if prev else "from the last 24 hours"
    nxt = "Evening roundup ~6pm GMT" if mode == "morning" else "Morning roundup ~7am GMT"
    body = [f'<div class="intro"><h1>{E(mode.title())} roundup</h1>'
            f"<p>{E(long_date(today))} · updated {clock(now)} GMT<br>"
            f"{len(clusters)} stories {E(since)} · {nxt}</p></div>",
            post_today_html(ranked, now)]
    jump = [("sec-today", "Post today", None)] if ranked else []

    for key, label in SECTIONS:
        group = by_section[key]
        if key == "breaking" and not group:
            continue
        body.append(h2(label, f"sec-{key}", len(group)))
        jump.append((f"sec-{key}", JUMP_LABELS[key], len(group)))
        if key in ("interviews", "trailers"):
            arts = [c for c in group if c["kind"] == "article"]
            vids = [c for c in group if c["kind"] == "video"]
            body.append(cluster_list(arts, now) if arts else "")
            if vids:
                body.append("<h3>" + ("Talk-show and podcast clips" if key == "interviews" else "Trailer and promo videos") + "</h3>")
                body.append(cluster_list(vids, now))
            if not arts and not vids:
                body.append('<p class="empty">Quiet.</p>')
            kinds = ("talk", "outlet") if key == "interviews" else ("streamer",)
            others = [v for v in other_videos if v.get("channel_kind") in kinds]
            if others:
                others.sort(key=lambda v: item_time(v), reverse=True)
                oc = build_clusters(others)
                body.append(f'<details class="other"><summary>Other uploads, not on the watchlist ({len(oc)})</summary>'
                            + "\n".join(cluster_html(c, now) for c in oc) + "</details>")
        elif key == "upcoming":
            body.append(cluster_list(group, now) if group or not ups else "")
            body.append(upcoming_html(ups))
        else:
            body.append(cluster_list(group, now))

    if mode == "morning":
        body.append(anniv_html(ann, today))
        jump.append(("sec-anniversaries", "Anniversaries", None))

    body.append(h2("Sources", "sec-sources"))
    body.append(health_html(health))
    jump.append(("sec-sources", "Sources", None))

    content = "\n".join(b for b in body if b)
    write_text(DOCS_DIR / "editions" / f"{name}.html",
               page(f"{title}: Sitcom Crave Roundup", content, depth=1, jump=jump))
    write_text(DOCS_DIR / "index.html",
               page(f"{title}: Sitcom Crave Roundup", content, depth=0, jump=jump))
    state["editions"] = [e for e in state.get("editions", []) if e["name"] != name]
    state["editions"].append({"name": name, "edition": mode, "time": iso(now)})
    return name


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(mode: str) -> int:
    global USER_AGENT
    now = now_utc()
    _SECRETS.extend([os.environ.get("TMDB_API_KEY", ""), os.environ.get("TELEGRAM_BOT_TOKEN", "")])
    cfg = json.loads((CONFIG_DIR / "sources.json").read_text(encoding="utf-8"))
    USER_AGENT = cfg.get("user_agent", USER_AGENT)
    wl = Watchlist(load_lines(CONFIG_DIR / "watchlist_shows.txt"),
                   load_lines(CONFIG_DIR / "watchlist_people.txt"))
    first_run = not STATE_FILE.exists()
    state = load_state()
    refilter(state, wl, cfg)
    log(f"{mode} run at {iso(now)}")

    raw, health = collect(mode, cfg, wl)
    added = merge(state, raw, wl, now)
    prune(state, now)
    log(f"{len(raw)} stories fetched, {added} new comedy stories, {len(state['items'])} remembered")

    # breaking: alert once per story, keep the breaking page current
    clusters = breaking_clusters(state, now)
    alerted = state.setdefault("alerted", {})
    fresh = [c for c in clusters if not any(m["id"] in alerted for m in c["members"])]
    sent = 0
    if first_run:
        log("First run: noting current breaking stories without alerting")
    else:
        for c in fresh[:MAX_ALERTS]:
            if send_telegram(alert_text(c)):
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
    if fresh or mode != "breaking" or not (DOCS_DIR / "breaking.html").exists():
        write_breaking_page(clusters, now)

    if mode == "breaking":
        if fresh:
            save_state(state)
        else:
            log("Nothing new and breaking; leaving files untouched")
        return 0

    name = build_edition(mode, state, health, now)
    write_archive(state)
    write_text(DATA_DIR / "health.json", json.dumps(health, indent=1))
    save_state(state)
    log(f"Wrote edition {name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["morning", "evening", "breaking"])
    args = ap.parse_args()
    return run(args.mode)


if __name__ == "__main__":
    sys.exit(main())
