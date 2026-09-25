#!/usr/bin/env python3
"""
Daily Motorcycle: RSS -> JSON -> Jev classification -> static-site data.

Commands:
  python dm.py validate            check every source in feeds.yaml, discover real feed URLs,
                                   write out/feed_report.csv
  python dm.py run [--mock]        fetch due feeds (conditional GET), classify new items, export
  python dm.py review              walk the review queue in the terminal (y/n per tag)
  python dm.py export              re-export JSON from the database only
  python dm.py stats               counts by status/category/source + your review decisions

Options: --force (ignore poll intervals), --feeds FILE, --config FILE.
--mock uses a keyword stand-in instead of the Jev API so the pipeline runs without a key.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import html
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import httpx
import yaml

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=value lines); real environment variables win."""
    if path.exists():
        for line in path.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_dotenv(ROOT / ".env")
ON_TOPIC = "_on_topic"   # reserved question keys
QUALITY = "_quality"


# ----------------------------------------------------------------------------
# Config / sources / storage
# ----------------------------------------------------------------------------

def load_config(path: Path, feeds_override: str | None = None) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg["_root"] = path.parent
    feeds_path = Path(feeds_override) if feeds_override else cfg["_root"] / cfg["feeds_file"]
    raw = yaml.safe_load(feeds_path.read_text())["sources"]
    cfg["_feeds_dir"] = feeds_path.parent
    cfg["sources"] = [resolve_source(s, cfg) for s in raw]
    return cfg


def resolve_source(s: dict, cfg: dict) -> dict:
    kind = s.get("kind", "article")
    k = cfg["kinds"].get(kind)
    if k is None:
        sys.exit(f"Unknown kind '{kind}' for source {s.get('name')}")
    return {
        "name": s["name"],
        "url": s["url"],
        "kind": kind,
        "media": k["media"],
        "poll_minutes": s.get("poll_minutes", k["poll_minutes"]),
        "gate": s.get("gate", k["gate"]),
        "ownership": s.get("ownership", "unknown"),
        "assign": s.get("assign", []) or [],
        "lang": s.get("lang", "en"),
        "enabled": s.get("enabled", True),
    }


ITEM_COLUMNS = {
    "id": "TEXT PRIMARY KEY", "source": "TEXT", "source_url": "TEXT", "kind": "TEXT",
    "media": "TEXT", "ownership": "TEXT", "lang": "TEXT", "title": "TEXT", "link": "TEXT",
    "author": "TEXT", "published": "TEXT", "summary": "TEXT", "image": "TEXT", "audio": "TEXT",
    "fetched_at": "TEXT", "scores": "TEXT", "on_topic": "REAL", "quality": "REAL",
    "categories": "TEXT", "pending": "TEXT", "status": "TEXT",
    "classified_by": "TEXT", "classified_at": "TEXT", "image_tried": "TEXT",
}
# status: new | published | review | offtopic | filtered


def db_connect(cfg: dict) -> sqlite3.Connection:
    con = sqlite3.connect(cfg["_root"] / cfg["db_path"])
    con.row_factory = sqlite3.Row
    cols = ", ".join(f"{k} {v}" for k, v in ITEM_COLUMNS.items())
    con.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS items ({cols});
        CREATE TABLE IF NOT EXISTS reviews (
            item_id TEXT, slug TEXT, score REAL, approved INTEGER, at TEXT,
            PRIMARY KEY (item_id, slug));
        CREATE TABLE IF NOT EXISTS story_pairs (
            a TEXT, b TEXT, cosine REAL, same_story REAL, at TEXT, PRIMARY KEY (a, b));
        CREATE TABLE IF NOT EXISTS feeds (
            url TEXT PRIMARY KEY, name TEXT, etag TEXT, modified TEXT,
            last_fetch TEXT, last_status INTEGER, last_error TEXT,
            last_new INTEGER, last_new_at TEXT);
        """
    )
    have = {r["name"] for r in con.execute("PRAGMA table_info(items)")}
    for k, v in ITEM_COLUMNS.items():          # migrate DBs from the first prototype
        if k not in have:
            con.execute(f"ALTER TABLE items ADD COLUMN {k} {v.replace(' PRIMARY KEY', '')}")
    con.commit()
    return con


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def is_remote(url: str) -> bool:
    return bool(re.match(r"https?://", url))


def local_path(url: str, cfg: dict) -> Path:
    p = Path(url)
    return p if p.is_absolute() else cfg["_feeds_dir"] / p


def http_client(cfg: dict) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=cfg.get("fetch_timeout_s", 20),
        headers={"User-Agent": cfg.get("user_agent", "DailyMotorcycleBot/0.2"),
                 "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"},
    )

# ----------------------------------------------------------------------------
# Language (rough): non-English posts skip Jev and go to a language page
# ----------------------------------------------------------------------------

STOPWORDS = {
    "en": "the and of to in is for with on that this are as at by from new it be you your has was will its not".split(),
    "fr": "le la les des du de et est une un pour dans sur avec qui que au aux en pas plus ce se sont par nous vous ses cette".split(),
    "sv": "och att det som en är på för med av inte den har till ett om vi kan från när så oss mer att vår våra ska".split(),
    "de": "der die das und ist nicht mit für auf ein eine den dem von zu im auch sich wie wird sind bei nach".split(),
    "es": "el los las que y en un una por con para es del se su más como pero sus este esta".split(),
    "it": "il lo gli di che per con non sono del della più anche come questo questa nel alla".split(),
    "nl": "de het een en van is op voor met niet dat zijn ook aan naar bij deze wordt".split(),
}
ACCENT_HINT = {"fr": "éèêàçùôîœ", "sv": "åäö", "de": "äöüß", "es": "ñ¿¡áíóú", "it": "àèìòù", "nl": "ĳ"}
LANG_PAGES = {
    "fr": {"slug": "french", "label": "Français"}, "sv": {"slug": "swedish", "label": "Svenska"},
    "de": {"slug": "german", "label": "Deutsch"}, "es": {"slug": "spanish", "label": "Español"},
    "it": {"slug": "italian", "label": "Italiano"}, "nl": {"slug": "dutch", "label": "Nederlands"},
}
OTHER_LANG = {"slug": "other-languages", "label": "Other Languages"}


def detect_lang(text: str) -> str | None:
    """Return a non-English language code when the text clearly is one, else None (treated as English)."""
    words = re.findall(r"[^\W\d_]+", (text or "").lower())
    if len(words) < 3:
        return None
    hits = {k: sum(w in set(v) for w in words) for k, v in STOPWORDS.items()}
    for k, chars in ACCENT_HINT.items():
        hits[k] += min(2, sum(text.lower().count(c) for c in chars) // 2)
    best = max((k for k in hits if k != "en"), key=lambda k: hits[k])
    if hits[best] >= 2 and hits[best] > hits["en"] + 1:
        return best
    return None


# ----------------------------------------------------------------------------
# Normalize
# ----------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
REDDIT_TAIL_RE = re.compile(r"submitted by\s+/u/\S+.*$", re.I | re.S)


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return WS_RE.sub(" ", s).strip()


def canonical_link(link: str) -> str:
    link = re.sub(r"[?&](utm_[^=]+|fbclid|gclid)=[^&#]*", "", link or "")
    return link.rstrip("?&").rstrip("/")


JUNK_IMG = re.compile(r"pixel|1x1|spacer|blank\.|gravatar|/emoji/|feedburner|doubleclick|/badge|logo", re.I)


def _img_from_html(markup: str) -> str | None:
    """First plausible article image in an HTML fragment (skips tracking pixels, icons, gravatars)."""
    for m in re.finditer(r"<img\b[^>]*>", markup or "", re.I):
        tag = m.group(0)
        w = re.search(r'\bwidth=["\']?(\d+)', tag)
        if w and int(w.group(1)) < 120:
            continue
        srcset = re.search(r'\bsrcset=["\']([^"\']+)', tag)
        src = re.search(r'\b(?:data-src|data-lazy-src|src)=["\']([^"\']+)', tag)
        url = None
        if srcset:                                  # biggest candidate wins
            cands = [c.strip().split() for c in html.unescape(srcset.group(1)).split(",") if c.strip()]
            cands = [(int(c[1][:-1]) if len(c) > 1 and c[1][:-1].isdigit() else 0, c[0]) for c in cands]
            url = max(cands)[1] if cands else None
        url = url or (html.unescape(src.group(1)) if src else None)
        if url and url.startswith("http") and not JUNK_IMG.search(url) and not url.startswith("data:"):
            return url
    return None


def first_image(entry) -> str | None:
    for key in ("media_thumbnail", "media_content"):
        for m in entry.get(key, []) or []:
            url = m.get("url")
            if url and not str(m.get("type", "image")).startswith(("audio", "video")) and not JUNK_IMG.search(url):
                return url
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    if entry.get("image", {}) and entry["image"].get("href"):
        return entry["image"]["href"]
    for c in entry.get("content", []) or []:          # WordPress puts the lead image in content:encoded
        url = _img_from_html(c.get("value", ""))
        if url:
            return url
    return _img_from_html(entry.get("summary", "") or "")


def first_audio(entry) -> str | None:
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("audio") and enc.get("href"):
            return enc["href"]
    return None


def entry_dt(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


@dataclass
class Item:
    id: str
    source: str
    source_url: str
    kind: str
    media: str
    ownership: str
    lang: str
    title: str
    link: str
    author: str
    published: str
    summary: str
    image: str | None
    audio: str | None


def normalize(entry, src: dict, cfg: dict) -> Item | None:
    link = canonical_link(entry.get("link", ""))
    if not link:
        return None
    body = entry.get("summary") or ""
    if entry.get("content"):
        body = entry["content"][0].get("value", body) or body
    text = clean_text(body)
    if src["kind"] == "reddit":
        text = REDDIT_TAIL_RE.sub("", text).strip()   # drop "submitted by /u/x [link] [comments]"
    if src["kind"] == "video" and not text:
        text = clean_text(entry.get("media_description") or "")
    dt = entry_dt(entry) or now()
    if (now() - dt).days > cfg.get("max_age_days", 90):
        return None                      # archive material (some feeds serve years of it); not news
    title = clean_text(entry.get("title"))
    lang = detect_lang(f"{title}. {text[:400]}") or src["lang"]
    return Item(
        id=hashlib.sha1(link.encode()).hexdigest(),
        source=src["name"], source_url=src["url"], kind=src["kind"], media=src["media"],
        ownership=src["ownership"], lang=lang,
        title=title, link=link,
        author=clean_text(entry.get("author")),
        published=dt.isoformat(),
        summary=text[: cfg.get("max_summary_chars", 1500)],
        image=first_image(entry), audio=first_audio(entry),
    )


# ----------------------------------------------------------------------------
# Fetch (conditional GET, per-source poll interval)
# ----------------------------------------------------------------------------

def is_due(con, src: dict, force: bool) -> bool:
    if force:
        return True
    r = con.execute("SELECT last_fetch FROM feeds WHERE url=?", (src["url"],)).fetchone()
    if not r or not r["last_fetch"]:
        return True
    last = datetime.fromisoformat(r["last_fetch"])
    return now() - last >= timedelta(minutes=src["poll_minutes"])


async def fetch_one(client, con, src: dict, cfg: dict) -> list[Item]:
    url = src["url"]
    prev = con.execute("SELECT etag, modified FROM feeds WHERE url=?", (url,)).fetchone()
    status, err, etag, modified, content = None, None, None, None, None
    if is_remote(url):
        headers = {}
        if prev and prev["etag"]:
            headers["If-None-Match"] = prev["etag"]
        if prev and prev["modified"]:
            headers["If-Modified-Since"] = prev["modified"]
        try:
            r = await client.get(url, headers=headers)
            status = r.status_code
            etag, modified = r.headers.get("etag"), r.headers.get("last-modified")
            if status == 200:
                content = r.content
            elif status != 304:
                err = f"HTTP {status}"
        except httpx.HTTPError as e:
            err = f"{type(e).__name__}: {e}"
    else:
        try:
            content, status = local_path(url, cfg).read_bytes(), 200
        except OSError as e:
            err = str(e)

    items: list[Item] = []
    if content is not None:
        parsed = feedparser.parse(content)
        if not parsed.entries and (parsed.bozo or not parsed.version):
            err = "not a feed (run validate)"
        for e in parsed.entries:
            it = normalize(e, src, cfg)
            if it:
                items.append(it)

    con.execute(
        """INSERT INTO feeds (url, name, etag, modified, last_fetch, last_status, last_error)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(url) DO UPDATE SET name=excluded.name,
             etag=COALESCE(excluded.etag, feeds.etag),
             modified=COALESCE(excluded.modified, feeds.modified),
             last_fetch=excluded.last_fetch, last_status=excluded.last_status,
             last_error=excluded.last_error""",
        (url, src["name"], etag, modified, now_iso(), status, err),
    )
    label = "304 unchanged" if status == 304 else (err or f"{len(items)} entries")
    print(f"  {src['name']:<30} {label}")
    return items


async def fetch_all(con, cfg: dict, force: bool) -> list[Item]:
    due = [s for s in cfg["sources"] if s["enabled"] and is_due(con, s, force)]
    skipped = sum(1 for s in cfg["sources"] if s["enabled"]) - len(due)
    if skipped:
        print(f"  ({skipped} sources not due yet; --force to poll anyway)")
    sem = asyncio.Semaphore(cfg.get("fetch_concurrency", 6))
    async with http_client(cfg) as client:
        async def go(s):
            async with sem:
                return await fetch_one(client, con, s, cfg)
        results = await asyncio.gather(*(go(s) for s in due))
    con.commit()
    return [it for batch in results for it in batch]


def insert_new(con, items: list[Item]) -> int:
    n = 0
    per_source: dict[str, int] = {}
    for it in items:
        cur = con.execute(
            """INSERT OR IGNORE INTO items
               (id, source, source_url, kind, media, ownership, lang, title, link, author,
                published, summary, image, audio, fetched_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
            (it.id, it.source, it.source_url, it.kind, it.media, it.ownership, it.lang, it.title,
             it.link, it.author, it.published, it.summary, it.image, it.audio, now_iso()),
        )
        if not cur.rowcount and it.image:               # already stored: backfill a missing image
            con.execute("UPDATE items SET image=? WHERE id=? AND (image IS NULL OR image='')", (it.image, it.id))
        n += cur.rowcount
        if cur.rowcount:
            per_source[it.source_url] = per_source.get(it.source_url, 0) + 1
    for url, k in per_source.items():
        con.execute("UPDATE feeds SET last_new=?, last_new_at=? WHERE url=?", (k, now_iso(), url))
    con.commit()
    return n


# ----------------------------------------------------------------------------
# Validate: is each source a live, current feed? If not, can we find the real one?
# ----------------------------------------------------------------------------

ALT_RE = re.compile(r"<link\b[^>]*>", re.I)


def discover_feed_links(page: str, base: str) -> list[str]:
    found = []
    for tag in ALT_RE.findall(page):
        if re.search(r'rel=["\']?alternate', tag, re.I) and re.search(r"(rss|atom)\+xml", tag, re.I):
            m = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if m:
                found.append(urljoin(base, html.unescape(m.group(1))))
    # YouTube channel page -> channel feed
    m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', page)
    if m:
        found.insert(0, f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}")
    # Directory pages (e.g. Yamaha /rss/) often just link to .xml/.rss files
    if not found:
        for href in re.findall(r'href=["\']([^"\']+\.(?:rss|xml|atom)(?:\?[^"\']*)?)["\']', page, re.I)[:5]:
            found.append(urljoin(base, html.unescape(href)))
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def feed_health(parsed, stale_days: int) -> dict:
    dates = sorted((d for d in (entry_dt(e) for e in parsed.entries) if d), reverse=True)
    newest = dates[0] if dates else None
    per_week = None
    if len(dates) >= 2:
        span_days = max(1.0, (dates[0] - dates[-1]).total_seconds() / 86400)
        per_week = round(len(dates) / span_days * 7, 1)
    age = (now() - newest).days if newest else None
    verdict = "OK"
    if not parsed.entries:
        verdict = "EMPTY"
    elif age is not None and age > stale_days:
        verdict = "STALE"
    return {"entries": len(parsed.entries), "newest": newest.date().isoformat() if newest else "",
            "age_days": age if age is not None else "", "per_week": per_week or "", "verdict": verdict,
            "full_text": any(e.get("content") for e in parsed.entries)}


async def validate_one(client, src: dict, cfg: dict) -> dict:
    row = {"name": src["name"], "kind": src["kind"], "enabled": src["enabled"], "url": src["url"],
           "status": "", "verdict": "", "entries": "", "newest": "", "age_days": "", "per_week": "",
           "full_text": "", "suggested_url": "", "note": ""}
    stale = cfg.get("stale_after_days", 180)
    try:
        if is_remote(src["url"]):
            r = await client.get(src["url"])
            row["status"] = r.status_code
            if r.status_code >= 400:
                row["verdict"] = "DEAD"
                row["note"] = f"HTTP {r.status_code}"
                return row
            body, final = r.content, str(r.url)
            if final.rstrip("/") != src["url"].rstrip("/"):
                row["note"] = f"redirects to {final}"
        else:
            body, final = local_path(src["url"], cfg).read_bytes(), src["url"]
            row["status"] = "file"
        parsed = feedparser.parse(body)
        if parsed.entries or (parsed.version and not parsed.bozo):
            row.update(feed_health(parsed, stale))
            return row
        # Not a feed. Try discovery on the HTML.
        row["verdict"] = "NOT_A_FEED"
        text = body.decode("utf-8", "replace")
        for cand in discover_feed_links(text, final)[:4]:
            try:
                r2 = await client.get(cand)
                p2 = feedparser.parse(r2.content)
                if r2.status_code < 400 and p2.entries:
                    h = feed_health(p2, stale)
                    row["suggested_url"] = cand
                    row["verdict"] = "FOUND_FEED"
                    row["note"] = f"discovered: {h['entries']} entries, newest {h['newest']}, {h['per_week']}/wk"
                    return row
            except httpx.HTTPError:
                continue
        row["note"] = "HTML page; no feed link found"
    except httpx.HTTPError as e:
        row["verdict"] = "ERROR"
        row["note"] = f"{type(e).__name__}: {e}"[:160]
    except OSError as e:
        row["verdict"] = "ERROR"
        row["note"] = str(e)[:160]
    return row


async def validate(cfg: dict) -> None:
    sem = asyncio.Semaphore(cfg.get("fetch_concurrency", 6))
    async with http_client(cfg) as client:
        async def go(s):
            async with sem:
                return await validate_one(client, s, cfg)
        rows = await asyncio.gather(*(go(s) for s in cfg["sources"]))

    order = {"DEAD": 0, "ERROR": 1, "NOT_A_FEED": 2, "FOUND_FEED": 3, "EMPTY": 4, "STALE": 5, "OK": 6}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), r["name"]))
    print(f"\n{'verdict':<11} {'kind':<8} {'posts/wk':>8} {'newest':<11} source")
    for r in rows:
        off = "" if r["enabled"] else "  (disabled)"
        extra = f"  -> {r['suggested_url']}" if r["suggested_url"] else ""
        extra += f"  [{r['note']}]" if r["note"] else ""
        print(f"{r['verdict']:<11} {r['kind']:<8} {str(r['per_week']):>8} {r['newest']:<11} {r['name']}{off}{extra}")

    out = cfg["_root"] / cfg["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "feed_report.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda x: order.get(x[0], 9))))
    print(f"Report: {out / 'feed_report.csv'}  (paste suggested_url values into feeds.yaml)")


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------

def src_lookup(cfg: dict) -> dict:
    return {s["url"]: s for s in cfg["sources"]}


def build_questions(cfg: dict, src: dict | None) -> dict:
    qs = {ON_TOPIC: {"type": "noul", "instructions": cfg["on_topic_instructions"].strip()}}
    if src and src["gate"]:
        qs[QUALITY] = {"type": "noul", "instructions": cfg["quality_instructions"].strip(),
                       "criteria": cfg["quality_criteria"]}
    assigned = set(src["assign"]) if src else set()
    for c in cfg["categories"]:
        if c["slug"] in assigned:
            continue            # source already tells us; don't pay to ask
        q = {"type": "noul", "instructions": c["instructions"].strip()}
        if c.get("criteria"):
            q["criteria"] = c["criteria"]
        qs[c["slug"]] = q
    return qs


def build_state(row) -> str:
    media = {"podcast": "Podcast episode", "video": "Video", "discussion": "Community discussion post",
             "listing": "Classified listing"}.get(row["media"], "Article")
    return f"{media} from {row['source']}\nTitle: {row['title']}\n\n{row['summary']}"


class JevClassifier:
    def __init__(self, cfg: dict):
        j = cfg["jev"]
        key = os.environ.get(j.get("api_key_env", "TYPESAFE_API_KEY"))
        if not key:
            sys.exit(f"Set {j.get('api_key_env')} (or use --mock).")
        self.url = j["base_url"].rstrip("/") + "/v1/systemone"
        self.model = j.get("model", "jev-latest")
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.sem = asyncio.Semaphore(j.get("concurrency", 8))
        self.timeout = j.get("timeout_s", 20)
        self.name = self.model

    async def classify(self, client, state: str, questions: dict) -> tuple[dict, str]:
        body = {"model": self.model, "state": state, "questions": questions}
        async with self.sem:
            for attempt in range(6):
                r = await client.post(self.url, json=body, headers=self.headers, timeout=self.timeout)
                if r.status_code in (429, 529, 502, 503):
                    await asyncio.sleep(min(30, 2 ** attempt) + random.random())
                    continue
                r.raise_for_status()
                data = r.json()
                return {k: float(v["noul"]) for k, v in data["answers"].items()}, data.get("model", self.model)
        raise RuntimeError("Jev: gave up after retries")


class MockClassifier:
    """Keyword stand-in so the pipeline runs without an API key. Says nothing about Jev accuracy."""

    KW = {
        ON_TOPIC: r"motorcycl|moto|bike|rider|motogp|wsbk|supercross|ducati|harley|honda|yamaha|kawasaki|bmw|ktm|triumph|helmet|cafe racer",
        QUALITY: r"build|restor|race report|how to|guide|review|news|announc|ride report|write.?up",
        "racing": r"motogp|wsbk|superbike championship|grand prix|race|podium|pole|supercross|motoamerica|isle of man|\btt\b",
        "adventure": r"adventure|\badv\b|dual.sport|overland|off.road|gravel|rally|tenere|africa twin",
        "electric": r"electric|battery|\bkwh\b|\bev\b|charging|zero motorcycles|livewire|energica",
        "new-models": r"unveil|revealed|reveal|new model|first look|first ride|launch|model year",
        "industry": r"sales|revenue|earnings|acquir|recall|tariff|regulat|ceo|bankrupt|layoff",
        "custom-vintage": r"custom|cafe racer|restor|vintage|classic|scrambler|bobber",
        "gear": r"helmet|jacket|boots|gloves|luggage|intercom|gear review",
    }

    name = "mock"

    async def classify(self, client, state: str, questions: dict) -> tuple[dict, str]:
        s = state.lower()
        out = {}
        for slug in questions:
            hits = len(re.findall(self.KW.get(slug, r"$^"), s))
            if slug in (ON_TOPIC, QUALITY):
                out[slug] = 0.9 if hits else 0.1
            else:
                out[slug] = round(min(0.97, 0.05 + 0.3 * hits), 3)
        return out, "mock"


def decide(scores: dict, src: dict | None, cfg: dict) -> tuple[list, list, str]:
    """Fit tiers: >= auto confident, >= review fuzzy (both published in that category).
    On-topic with nothing >= review is published with no category and shows in the 'more' lane."""
    th = cfg["thresholds"]
    if scores.get(ON_TOPIC, 1.0) < th["on_topic"]:
        return [], [], "offtopic"
    if src and src["gate"] and scores.get(QUALITY, 1.0) < th["quality"]:
        return [], [], "filtered"
    cats = [c["slug"] for c in cfg["categories"]]
    assigned = [s for s in (src["assign"] if src else []) if s in cats]
    fits = sorted((s for s in cats if s not in assigned and scores.get(s, 0) >= th["review"]),
                  key=lambda s: -scores[s])
    return assigned + fits, [], "published"


async def classify_new(con, cfg: dict, clf) -> int:
    con.execute("""UPDATE items SET status='published', categories='[]', pending='[]',
                   classified_by='language-rule', classified_at=? WHERE status='new' AND lang != 'en'""", (now_iso(),))
    rows = con.execute("SELECT * FROM items WHERE status='new'").fetchall()
    if not rows:
        print("  nothing new to classify")
        return 0
    sources = src_lookup(cfg)
    t0 = time.perf_counter()
    async with httpx.AsyncClient() as client:
        async def one(row):
            src = sources.get(row["source_url"])
            try:
                scores, model = await clf.classify(client, build_state(row), build_questions(cfg, src))
            except Exception as e:   # leave as 'new' so the next run retries
                print(f"  ! {row['title'][:60]}: {e}", file=sys.stderr)
                return
            for s in (src["assign"] if src else []):
                scores[s] = 1.0
            auto, pending, status = decide(scores, src, cfg)
            con.execute(
                """UPDATE items SET scores=?, on_topic=?, quality=?, categories=?, pending=?, status=?,
                   classified_by=?, classified_at=? WHERE id=?""",
                (json.dumps(scores), scores.get(ON_TOPIC), scores.get(QUALITY), json.dumps(auto),
                 json.dumps(pending), status, model, now_iso(), row["id"]),
            )
        await asyncio.gather(*(one(r) for r in rows))
    con.commit()
    print(f"  classified {len(rows)} items in {time.perf_counter() - t0:.2f}s via {clf.name}")
    return len(rows)


# ----------------------------------------------------------------------------
# Export (what the static site reads)
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Image fallback: og:image from the article page, once per item
# ----------------------------------------------------------------------------

OG_RE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*>', re.I)
CONTENT_RE = re.compile(r'content=["\']([^"\']+)["\']', re.I)


async def enrich_images(con, cfg: dict, limit: int = 400) -> int:
    """Items whose feed carried no image get the page's og:image. One polite GET per item, ever."""
    rows = con.execute("""SELECT id, link FROM items WHERE status='published' AND (image IS NULL OR image='')
                          AND image_tried IS NULL AND lang='en' ORDER BY published DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return 0
    sem = asyncio.Semaphore(cfg.get("image_concurrency", 5))
    found = 0
    async with http_client(cfg) as client:
        async def one(r):
            nonlocal found
            url = None
            async with sem:
                try:
                    resp = await client.get(r["link"], headers={"Accept": "text/html"})
                    if resp.status_code == 200:
                        for tag in OG_RE.findall(resp.text[:200_000]):
                            m = CONTENT_RE.search(tag)
                            if m:
                                url = urljoin(str(resp.url), html.unescape(m.group(1)))
                                break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            con.execute("UPDATE items SET image=COALESCE(?, image), image_tried=? WHERE id=?", (url, now_iso(), r["id"]))
            found += bool(url)
        await asyncio.gather(*(one(r) for r in rows))
    con.commit()
    print(f"  page images: {found}/{len(rows)} found via og:image")
    return found


# ----------------------------------------------------------------------------
# Story grouping: the same news covered by several outlets becomes one item
# ----------------------------------------------------------------------------
# Stage 1 (free): tf-idf cosine over title + lede finds candidate pairs from different sources within a
# few days. Stage 2: Jev answers "same story?" for each candidate; answers are cached in story_pairs.
# Export then collapses each cluster to one lead item with "also covered by" links.

STORY_STOP = set(STOPWORDS["en"]) | set("about after into over more than their they what when will your just says said get gets new".split())


def story_tokens(row) -> dict:
    words = re.findall(r"[a-z0-9][a-z0-9\-]{2,}", f"{row['title']} {row['title']} {(row['summary'] or '')[:300]}".lower())
    tf: dict[str, int] = {}
    for w in words:
        if w not in STORY_STOP:
            tf[w] = tf.get(w, 0) + 1
    return tf


def candidate_pairs(rows, cfg: dict) -> list[tuple]:
    st = cfg.get("stories", {})
    days, min_cos = st.get("window_days", 4), st.get("min_cosine", 0.22)
    docs = {r["id"]: story_tokens(r) for r in rows}
    df: dict[str, int] = {}
    for tf in docs.values():
        for w in tf:
            df[w] = df.get(w, 0) + 1
    n = len(rows)
    idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}
    vec, norm = {}, {}
    for i, tf in docs.items():
        v = {w: c * idf[w] for w, c in tf.items() if df[w] < max(4, n * 0.05)}     # drop near-universal words
        vec[i], norm[i] = v, math.sqrt(sum(x * x for x in v.values())) or 1.0
    inv: dict[str, list] = {}
    for r in rows:
        for w in vec[r["id"]]:
            inv.setdefault(w, []).append(r["id"])
    meta = {r["id"]: r for r in rows}
    seen, out = set(), []
    for r in rows:
        shared: dict[str, float] = {}
        for w, x in vec[r["id"]].items():
            for j in inv[w]:
                if j > r["id"]:
                    shared[j] = shared.get(j, 0.0) + x * vec[j][w]
        for j, dot in shared.items():
            o = meta[j]
            if o["source"] == r["source"]:
                continue
            if abs(_ts(r["published"]) - _ts(o["published"])) > days * 86400:
                continue
            cos = dot / (norm[r["id"]] * norm[j])
            if cos >= min_cos and (r["id"], j) not in seen:
                seen.add((r["id"], j)); out.append((r["id"], j, cos))
    return out


def pair_state(a, b) -> str:
    def one(x):
        return f"{x['source']}: {x['title']}\n{(x['summary'] or '')[:300]}"
    return f"Article A ({one(a)})\n\nArticle B ({one(b)})"


async def group_new(con, cfg: dict, clf) -> int:
    """Ask Jev about candidate pairs we have not judged yet."""
    st = cfg.get("stories", {})
    rows = con.execute("SELECT id, title, summary, source, published FROM items WHERE status='published' AND lang='en'").fetchall()
    known = {(r["a"], r["b"]) for r in con.execute("SELECT a, b FROM story_pairs")}
    cands = [c for c in candidate_pairs(rows, cfg) if (c[0], c[1]) not in known]
    if not cands:
        print("  no new candidate story pairs")
        return 0
    meta = {r["id"]: r for r in rows}
    q = {"_same_story": {"type": "noul", "instructions": st.get("instructions", "").strip()}}
    t0 = time.perf_counter()
    async with httpx.AsyncClient() as client:
        async def one(c):
            a, b, cos = c
            try:
                scores, _ = await clf.classify(client, pair_state(meta[a], meta[b]), q)
            except Exception as e:
                print(f"  ! pair {a[:8]}/{b[:8]}: {e}", file=sys.stderr)
                return
            con.execute("INSERT OR REPLACE INTO story_pairs VALUES (?,?,?,?,?)", (a, b, cos, scores.get("_same_story"), now_iso()))
        await asyncio.gather(*(one(c) for c in cands))
    con.commit()
    print(f"  judged {len(cands)} candidate story pairs in {time.perf_counter() - t0:.2f}s")
    return len(cands)


def collapse_stories(items: list[dict], pairs: list, threshold: float) -> list[dict]:
    """Merge items joined by confirmed same-story pairs; the lead is the quietest source's item."""
    by_id = {i["id"]: i for i in items}
    parent = {i: i for i in by_id}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b, score in pairs:
        if score is not None and score >= threshold and a in by_id and b in by_id:
            parent[find(a)] = find(b)
    groups: dict[str, list] = {}
    for i in items:
        groups.setdefault(find(i["id"]), []).append(i)
    volume: dict[str, int] = {}
    for i in items:
        volume[i["source"]] = volume.get(i["source"], 0) + 1
    out = []
    for members in groups.values():
        if len(members) == 1:
            members[0]["also"] = []
            out.append(members[0])
            continue
        lead = min(members, key=lambda m: (volume[m["source"]], -len(m["excerpt"] or ""), m["published"]))
        lead["also"] = [{"source": m["source"], "title": m["title"], "link": m["link"], "published": m["published"]}
                        for m in sorted(members, key=lambda m: m["published"]) if m is not lead]
        out.append(lead)
    return out


# ----------------------------------------------------------------------------
# Ranking: fairness across sources, not volume
# ----------------------------------------------------------------------------

def rank_items(items: list[dict], window_days: int = 21, slug: str | None = None, fuzzy_penalty: int = 3) -> list[dict]:
    """Order items so no source can win the front page by volume.

    Each source's items are ordered newest-first and given a position r (0 = its newest). Items sort by
    r first, so every source's newest comes before any source's second-newest, and so on. Within a round
    the rarest sources (fewest items in the set) go first: quiet, distinctive sources surface early.
    On a category page (slug given) a fuzzy fit (score < 0.85 for that category) is treated as if it sat
    fuzzy_penalty positions later in its source's list: still shown, but confident fits lead.
    Items older than window_days (relative to the newest item) go after everything else, newest-first.
    """
    if not items:
        return []
    newest = max(i["published"] for i in items)
    cutoff = (datetime.fromisoformat(newest) - timedelta(days=window_days)).isoformat()
    fresh = [i for i in items if i["published"] >= cutoff]
    stale = sorted((i for i in items if i["published"] < cutoff), key=lambda i: i["published"], reverse=True)
    by_src: dict[str, list[dict]] = {}
    for i in sorted(fresh, key=lambda i: i["published"], reverse=True):
        by_src.setdefault(i["source"], []).append(i)
    volume = {k: len(v) for k, v in by_src.items()}
    def penalty(it):
        return fuzzy_penalty if slug and it.get("cat_scores", {}).get(slug, 1.0) < 0.85 else 0
    ranked = [(r + penalty(it), volume[k], -_ts(it["published"]), it) for k, v in by_src.items() for r, it in enumerate(v)]
    ranked.sort(key=lambda t: t[:3])
    return [t[3] for t in ranked] + stale


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


MORE = {"slug": "odd-ball", "label": "Odd Ball"}


def row_to_public(row, labels: dict) -> dict:
    cats = json.loads(row["categories"] or "[]")
    scores = json.loads(row["scores"] or "{}")
    top = max((scores.get(c, 0) for c in cats), default=0)
    lang_page = None if row["lang"] == "en" else LANG_PAGES.get(row["lang"], OTHER_LANG)
    cat_scores = {c: round(scores.get(c, 0), 2) for c in cats}
    return {
        "id": row["id"], "title": row["title"], "link": row["link"], "lang": row["lang"],
        "source": row["source"], "ownership": row["ownership"], "media": row["media"],
        "author": row["author"] or None, "published": row["published"],
        "excerpt": (row["summary"] or "")[:280], "image": row["image"], "audio": row["audio"],
        "categories": [lang_page] if lang_page else [{"slug": c, "label": labels[c]} for c in cats if c in labels] or [MORE],
        "primary": lang_page["slug"] if lang_page else (cats[0] if cats else MORE["slug"]),
        "confident": top >= 0.85 if cats and not lang_page else False,
        "cat_scores": cat_scores,
    }


def export(con, cfg: dict) -> None:
    out = cfg["_root"] / cfg["output_dir"]
    (out / "categories").mkdir(parents=True, exist_ok=True)
    labels = {c["slug"]: c["label"] for c in cfg["categories"]}
    pages = {**labels, MORE["slug"]: MORE["label"],
             **{p["slug"]: p["label"] for p in [*LANG_PAGES.values(), OTHER_LANG]}}   # + odd-ball and language pages
    rows = con.execute(
        """SELECT * FROM items WHERE status IN ('published','review')
           ORDER BY published DESC LIMIT ?""",
        (cfg.get("export_limit", 500),),
    ).fetchall()
    window = cfg.get("ranking", {}).get("window_days", 21)
    public = [row_to_public(r, labels) for r in rows]
    pairs = [(p["a"], p["b"], p["same_story"]) for p in con.execute("SELECT a, b, same_story FROM story_pairs")]
    public = collapse_stories(public, pairs, cfg.get("stories", {}).get("same_story_threshold", 0.85))
    items = rank_items(public, window)
    (out / "items.json").write_text(json.dumps(items, indent=2))

    for slug, label in pages.items():
        sub = rank_items([i for i in items if any(c["slug"] == slug for c in i["categories"])], window, slug)
        (out / "categories" / f"{slug}.json").write_text(
            json.dumps({"slug": slug, "label": label, "items": sub}, indent=2))
    (out / "categories.json").write_text(json.dumps(
        [{"slug": s, "label": l, "count": sum(1 for i in items if any(c["slug"] == s for c in i["categories"]))}
         for s, l in pages.items()], indent=2))

    # Source directory: part of the point is sending people to the sources themselves.
    src_counts = {r["source"]: r["n"] for r in con.execute(
        "SELECT source, COUNT(*) n FROM items WHERE status='published' GROUP BY source")}
    (out / "sources.json").write_text(json.dumps(
        [{"name": s["name"], "kind": s["kind"], "media": s["media"], "ownership": s["ownership"],
          "published_items": src_counts.get(s["name"], 0)}
         for s in cfg["sources"] if s["enabled"]], indent=2))

    queue = con.execute(
        "SELECT id, title, source, scores, categories, pending FROM items WHERE status='review' ORDER BY published DESC"
    ).fetchall()
    (out / "review_queue.json").write_text(json.dumps(
        [{"id": q["id"], "title": q["title"], "source": q["source"],
          "applied": json.loads(q["categories"] or "[]"), "pending": json.loads(q["pending"] or "[]"),
          "scores": json.loads(q["scores"] or "{}")} for q in queue], indent=2))
    print(f"  exported {len(items)} items, {len(queue)} in review -> {out}/")


# ----------------------------------------------------------------------------
# Review loop
# ----------------------------------------------------------------------------

def review(con, cfg: dict) -> None:
    labels = {c["slug"]: c["label"] for c in cfg["categories"]}
    rows = con.execute("SELECT * FROM items WHERE status='review' ORDER BY published DESC").fetchall()
    if not rows:
        print("Review queue is empty.")
        return
    print(f"{len(rows)} items to review. Answer y/n per tag, s = skip item, q = quit.\n")
    for row in rows:
        scores = json.loads(row["scores"] or "{}")
        cats = json.loads(row["categories"] or "[]")
        pending = json.loads(row["pending"] or "[]")
        if not cats and not pending:
            pending = sorted(labels, key=lambda s: -scores.get(s, 0))[:3]
        print(f"— {row['title']}  [{row['source']}]")
        print(f"  {(row['summary'] or '')[:200]}…")
        if cats:
            print(f"  already tagged: {', '.join(labels.get(c, c) for c in cats)}")
        skip = False
        for slug in pending:
            try:
                ans = input(f"  {labels[slug]} ({scores.get(slug, 0):.2f})? [y/n/s/q] ").strip().lower()
            except EOFError:
                ans = "q"
            if ans == "q":
                con.commit(); export(con, cfg); return
            if ans == "s":
                skip = True; break
            ok = ans == "y"
            con.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?)",
                        (row["id"], slug, scores.get(slug, 0), int(ok), now_iso()))
            if ok and slug not in cats:
                cats.append(slug)
        if not skip:
            con.execute("UPDATE items SET categories=?, pending='[]', status=? WHERE id=?",
                        (json.dumps(cats), "published" if cats else "offtopic", row["id"]))
        print()
    con.commit()
    export(con, cfg)


def stats(con, cfg: dict) -> None:
    print("By status:")
    for r in con.execute("SELECT status, COUNT(*) n FROM items GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:<10} {r['n']}")
    print("By category (auto + assigned + approved):")
    counts: dict[str, int] = {}
    for r in con.execute("SELECT categories FROM items WHERE categories IS NOT NULL"):
        for c in json.loads(r["categories"]):
            counts[c] = counts.get(c, 0) + 1
    for c in cfg["categories"]:
        print(f"  {c['label']:<22} {counts.get(c['slug'], 0)}")
    print("By source (published / total / filtered+offtopic):")
    for r in con.execute(
        """SELECT source, SUM(status='published') p, COUNT(*) n,
                  SUM(status IN ('filtered','offtopic')) x
           FROM items GROUP BY source ORDER BY p DESC"""):
        print(f"  {r['source']:<30} {r['p']:>4} / {r['n']:<4} {r['x']:>4}")
    rv = con.execute("SELECT slug, COUNT(*) n, SUM(approved) yes, AVG(score) s FROM reviews GROUP BY slug").fetchall()
    if rv:
        print("Your review decisions:")
        for r in rv:
            print(f"  {r['slug']:<16} reviewed {r['n']:>3}  approved {r['yes']:>3}  avg score {r['s']:.2f}")


# ----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["validate", "run", "group", "review", "export", "stats"])
    ap.add_argument("--mock", action="store_true", help="keyword stand-in instead of Jev")
    ap.add_argument("--force", action="store_true", help="ignore poll intervals")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--feeds", default=None, help="alternate feeds file")
    args = ap.parse_args()

    cfg = load_config(Path(args.config), args.feeds)

    if args.command == "validate":
        asyncio.run(validate(cfg))
        return
    con = db_connect(cfg)
    if args.command == "run":
        print("Fetching feeds…")
        n = insert_new(con, asyncio.run(fetch_all(con, cfg, args.force)))
        print(f"  {n} new items")
        clf = MockClassifier() if args.mock else JevClassifier(cfg)
        asyncio.run(classify_new(con, cfg, clf))
        asyncio.run(group_new(con, cfg, clf))
        asyncio.run(enrich_images(con, cfg))
        export(con, cfg)
    elif args.command == "group":
        clf = MockClassifier() if args.mock else JevClassifier(cfg)
        asyncio.run(group_new(con, cfg, clf))
        export(con, cfg)
    elif args.command == "review":
        review(con, cfg)
    elif args.command == "export":
        export(con, cfg)
    elif args.command == "stats":
        stats(con, cfg)


if __name__ == "__main__":
    main()
