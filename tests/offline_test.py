"""
Runs the whole roundup offline against made-up sample feeds (fictional shows
and people), so the pipeline can be checked without internet access.

    python tests/offline_test.py [output_dir]
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="roundup-test-"))
if OUT.exists():
    shutil.rmtree(OUT)
(OUT / "config").mkdir(parents=True)
shutil.copy(REPO / "config" / "sources.json", OUT / "config" / "sources.json")
(OUT / "config" / "watchlist_shows.txt").write_text("Maple Street\nGhosts*\nHarbor Lights\n")
(OUT / "config" / "watchlist_people.txt").write_text("Dana Fictional\n")
os.environ["ROUNDUP_ROOT"] = str(OUT)
os.environ["GITHUB_REPOSITORY"] = "aduseii/crave-roundup"
os.environ["TMDB_API_KEY"] = "test-key-123"
sys.path.insert(0, str(REPO))

import roundup  # noqa: E402

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Deadline</title>
{items}</channel></rss>"""
ITEM = """<item><title>{t}</title><link>{l}</link><pubDate>{d}</pubDate>
<description><![CDATA[<p>{s}</p>]]></description></item>"""

MORNING_TRADE = [
    ("&#8216;Maple Street&#8217; Renewed For Season 4 At NBC", "https://deadline.example/maple-renewed",
     "Sat, 07 Nov 2026 02:00:00 GMT", "NBC has renewed the comedy series for a fourth season. The post X appeared first on Deadline."),
    ("Dana Fictional Joins &#8216;Harbor Lights&#8217; Comedy Pilot", "https://deadline.example/dana-joins",
     "Sat, 07 Nov 2026 01:00:00 GMT", "The comedian will star in the multi-camera pilot."),
    ("&#8216;Ghosts&#8217; Sets Holiday Episode", "https://deadline.example/ghosts-holiday",
     "Fri, 06 Nov 2026 22:00:00 GMT", "The CBS comedy will air a holiday hour."),
    ("Ghosts Of The Past Haunt The Box Office", "https://deadline.example/box-office",
     "Fri, 06 Nov 2026 21:00:00 GMT", "Weekend numbers are in."),
    ("Drama &#8216;Steel City&#8217; Gets Premiere Date", "https://deadline.example/steel-city",
     "Fri, 06 Nov 2026 20:00:00 GMT", "The crime drama returns in March."),
    ("&#8216;Maple Street&#8217; Old Story", "https://deadline.example/old",
     "Sun, 01 Nov 2026 20:00:00 GMT", "An older sitcom story."),
    ("Comedy Series &#8216;Night Shift Diner&#8217; Trailer Released", "https://deadline.example/diner-trailer",
     "Sat, 07 Nov 2026 03:00:00 GMT", "Watch the first trailer for the series."),
    ("Dana Fictional Opens Up About &#8216;Maple Street&#8217; Early Days", "https://deadline.example/dana-interview",
     "Sat, 07 Nov 2026 04:00:00 GMT", "In a new interview, the star recalls the pilot."),
]
BREAKING_EXTRA = [
    ("Sitcom Actor Pat Example Dies At 81", "https://deadline.example/pat-example",
     "Sat, 07 Nov 2026 12:50:00 GMT", "The actor was known for sitcoms in the 1980s."),
]

GNEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title>
<item><title>NBC Renews 'Maple Street' for Season 4 - Variety</title>
<link>https://news.google.example/rss/articles/abc</link><pubDate>Sat, 07 Nov 2026 02:30:00 GMT</pubDate>
<source url="https://variety.com">Variety</source></item>
<item><title>'Back Lot' leaving Netflix this month - Decider</title>
<link>https://news.google.example/rss/articles/def</link><pubDate>Sat, 07 Nov 2026 05:00:00 GMT</pubDate>
<source url="https://decider.com">Decider</source></item>
</channel></rss>"""

YT = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
<title>The Tonight Show Starring Jimmy Fallon</title>
<entry><yt:videoId>vid1</yt:videoId><title>Dana Fictional Talks 'Maple Street' Renewal</title>
<link rel="alternate" href="https://www.youtube.com/watch?v=vid1"/><published>2026-11-07T04:30:00+00:00</published>
<media:group><media:description>Dana stops by to talk about the show.</media:description></media:group></entry>
<entry><yt:videoId>vid2</yt:videoId><title>Monologue: Weekend News</title>
<link rel="alternate" href="https://www.youtube.com/watch?v=vid2"/><published>2026-11-07T04:00:00+00:00</published>
<media:group><media:description>Tonight's monologue.</media:description></media:group></entry>
</feed>"""


def wd(rows):
    return {"results": {"bindings": [{k: {"value": v} for k, v in r.items()} for r in rows]}}


WD_BIRTHDAYS = wd([{"person": "http://www.wikidata.org/entity/Q900001", "name": "Test Person",
                    "date": "1981-11-07T00:00:00Z", "links": "50", "gender": "female",
                    "article": "https://en.wikipedia.org/wiki/Test_Person",
                    "shows": "Maple Street@@2019|Old Show@@2005|Old Show@@2006"}])
WD_DEATHS = wd([{"person": "http://www.wikidata.org/entity/Q900002", "name": "Old Actor",
                 "date": "2005-11-07T00:00:00Z", "born": "1930-03-02T00:00:00Z", "links": "30",
                 "gender": "male", "shows": "Classic Sitcom@@1990"}])
WD_PREMIERES = wd([{"show": "http://www.wikidata.org/entity/Q900003", "name": "Classic Sitcom",
                    "date": "1996-11-07T00:00:00Z", "links": "40", "networks": "NBC",
                    "article": "https://en.wikipedia.org/wiki/Classic_Sitcom"}])
WD_FINALES = wd([])

TMDB_NEW = {"results": [{"id": 11, "name": "New Comedy", "first_air_date": "2026-11-20"}]}
TMDB_RET = {"results": [{"id": 12, "name": "Returning Comedy"}, {"id": 13, "name": "Mid Season"}]}
TMDB_DETAIL = {
    11: {"name": "New Comedy", "first_air_date": "2026-11-20", "networks": [{"name": "Netflix"}],
         "overview": "A workplace comedy about a diner."},
    12: {"name": "Returning Comedy", "networks": [{"name": "ABC"}], "overview": "Season three.",
         "next_episode_to_air": {"episode_number": 1, "season_number": 3, "air_date": "2026-11-15"}},
    13: {"name": "Mid Season", "networks": [{"name": "CBS"}], "overview": "",
         "next_episode_to_air": {"episode_number": 7, "season_number": 2, "air_date": "2026-11-12"}},
}


class Resp:
    def __init__(self, body):
        self._body = body
        self.content = body.encode() if isinstance(body, str) else json.dumps(body).encode()

    def json(self):
        return self._body if not isinstance(self._body, str) else json.loads(self._body)


PHASE = {"trade": MORNING_TRADE}
CALLS = []


def fake_get(url, *, params=None, headers=None, timeout=25):
    params = params or {}
    CALLS.append((url, dict(params)))
    if "deadline.com" in url:
        return Resp(RSS.format(items="".join(ITEM.format(t=t, l=l, d=d, s=s) for t, l, d, s in PHASE["trade"])))
    if "news.google.com" in url:
        return Resp(GNEWS)
    if "youtube.com" in url:
        if params.get("channel_id") == "UC8-Th83bH_thdKZDJCrn88g":
            return Resp(YT)
        raise roundup.FetchError("HTTP 404")
    if "wikidata" in url:
        q = params["query"]
        if "P569/psv:P569" in q:
            return Resp(WD_BIRTHDAYS)
        if "P570/psv:P570" in q:
            return Resp(WD_DEATHS)
        if "P580/psv:P580" in q:
            return Resp(WD_PREMIERES)
        return Resp(WD_FINALES)
    if "themoviedb" in url:
        assert params.get("api_key") == "test-key-123"
        if url.endswith("/discover/tv"):
            if "first_air_date.gte" in params:
                return Resp(TMDB_NEW)
            return Resp(TMDB_RET if params.get("page") == 1 else {"results": []})
        sid = int(url.rsplit("/", 1)[-1])
        return Resp(TMDB_DETAIL[sid])
    raise roundup.FetchError("HTTP 403 (test: source not reachable)")


ALERTS = []
roundup.http_get = fake_get
roundup.send_telegram = lambda text: ALERTS.append(text) or True
roundup.time.sleep = lambda s: None


def run(mode, when):
    os.environ["ROUNDUP_NOW"] = when
    print(f"\n===== {mode} at {when} =====")
    roundup._SECRETS.clear()
    assert roundup.run(mode) == 0


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        check.failed += 1


check.failed = 0

run("morning", "2026-11-07T06:50:00Z")
idx = (OUT / "docs" / "index.html").read_text()
sections = {}
cur = None
for line in idx.splitlines():
    if line.startswith("<h2"):
        cur = line
    elif cur:
        sections.setdefault(cur, []).append(line)
sec = {k: "\n".join(v) for k, v in sections.items()}


def in_section(title, text):
    return any(title in k and text in v for k, v in sec.items())


check(in_section("Breaking", "Maple Street\u2019 Renewed"), "renewal is in Breaking")
check("2 outlets" in idx and ">Variety<" in idx, "renewal merged with the Variety copy from Google News")
check(in_section("News", "Harbor Lights"), "casting story is in News")
check("Ghosts\u2019 Sets Holiday" in idx, "quoted 'Ghosts' matched")
check("Box Office" not in idx, "unquoted 'Ghosts' headline dropped")
check("Steel City" not in idx, "drama story dropped")
check("Old Story" not in idx, "old story dropped")
check(in_section("Streaming", "Back Lot"), "leaving-Netflix story is in Streaming")
check("[Exit]" not in idx, "leaving Netflix is not tagged as an exit")
check(in_section("Trailers", "Night Shift Diner"), "trailer is in Trailers")
check(in_section("Interviews", "Opens Up"), "interview article is in Interviews")
check(in_section("Interviews", "Talks &#x27;Maple Street&#x27; Renewal"), "talk-show clip is in Interviews")
check("Other uploads" in idx and "Monologue" in idx, "unmatched talk-show upload is tucked away")
check("Test Person, turns 45 today" in idx, "birthday listed with the right age")
check("Old Actor, passed away 21 years ago today" in idx, "death anniversary listed")
check("&#x27;Classic Sitcom&#x27; premiered 30 years ago today" in idx, "premiere anniversary listed")
check("Returning Comedy" in idx and "Season 3 premiere" in idx, "TMDB season premiere listed")
check("Mid Season" not in idx, "mid-season episode is not called a premiere")
check("New Comedy" in idx, "TMDB new series listed")
check("Post today (ranked, times GMT)" in idx and "~8:00am" in idx, "post-today list with GMT times")
check("test-key-123" not in idx and "test-key-123" not in (OUT / "data" / "state.json").read_text(),
      "TMDB key never written to pages or data")
check("FAILED" in idx and "Source health" in idx, "failed sources shown in source health")
check(ALERTS == [], "first run does not send alerts")
check((OUT / "docs" / "editions" / "2026-11-07-morning.html").exists(), "edition page saved")
check('src="../app.js"' in (OUT / "docs" / "editions" / "2026-11-07-morning.html").read_text(),
      "edition page points at app.js correctly")

PHASE["trade"] = MORNING_TRADE + BREAKING_EXTRA
run("breaking", "2026-11-07T13:13:00Z")
check(len(ALERTS) == 1 and "Pat Example" in ALERTS[0], "breaking check sends one alert for the new story")
check("aduseii.github.io/crave-roundup/breaking.html" in (ALERTS[0] if ALERTS else ""),
      "alert links to the breaking page")
brk = (OUT / "docs" / "breaking.html").read_text()
check("Pat Example" in brk and "Maple Street" in brk, "breaking page lists both breaking stories")

run("breaking", "2026-11-07T13:43:00Z")
check(len(ALERTS) == 1, "the same story is not alerted twice")

run("evening", "2026-11-07T17:50:00Z")
eve = (OUT / "docs" / "index.html").read_text()
check("Evening roundup" in eve and "Pat Example" in eve, "evening edition has the afternoon story")
check("Renewed For Season 4" not in eve, "evening edition leaves out what the morning had")
check("Anniversaries" not in eve, "anniversaries only in the morning")
arc = (OUT / "docs" / "archive.html").read_text()
check("2026-11-07-morning.html" in arc and "2026-11-07-evening.html" in arc, "archive lists both editions")

print(f"\nOutput in {OUT}")
print("ALL PASSED" if not check.failed else f"{check.failed} FAILED")
sys.exit(1 if check.failed else 0)
