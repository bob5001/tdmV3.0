#!/usr/bin/env python3
"""
Daily Motorcycle — RSS -> JSON -> Jev classification -> static-site data.

Commands:
  python dm.py run [--mock]      fetch feeds, classify new items, export JSON
  python dm.py review            walk the review queue in the terminal (y/n per tag)
  python dm.py export            re-export JSON from the database only
  python dm.py stats             counts by status/category + agreement with your reviews

--mock uses a keyword stand-in instead of the Jev API, so the whole pipeline
can be exercised without a key. Real runs need TYPESAFE_API_KEY in the env.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
import yaml

ROOT = Path(__file__).resolve().parent
ON_TOPIC = "_on_topic"  # reserved question key


# ----------------------------------------------------------------------------
# Config / storage
# ----------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg["_root"] = path.parent
    return cfg


def db_connect(cfg: dict) -> sqlite3.Connection:
    con = sqlite3.connect(cfg["_root"] / cfg["db_path"])
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            id            TEXT PRIMARY KEY,   -- sha1 of canonical link
            source        TEXT,
            title         TEXT,
            link          TEXT,
            published     TEXT,               -- ISO 8601 UTC
            summary       TEXT,
            image         TEXT,
            fetched_at    TEXT,
            scores        TEXT,               -- JSON {slug: p}
            on_topic      REAL,
            categories    TEXT,               -- JSON [slug] auto-applied or approved
            pending       TEXT,               -- JSON [slug] awaiting review
            status        TEXT,               -- new | published | review | offtopic
            classified_by TEXT,               -- jev model id or 'mock'
            classified_at TEXT
        );
        CREATE TABLE IF NOT EXISTS reviews (  -- your decisions = eval/tuning data
            item_id  TEXT,
            slug     TEXT,
            score    REAL,
            approved INTEGER,
            at       TEXT,
            PRIMARY KEY (item_id, slug)
        );
        """
    )
    return con


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------------------
# Fetch + normalize
# ----------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return WS_RE.sub(" ", s).strip()


def canonical_link(link: str) -> str:
    # strip tracking params so the same story from two feeds dedupes
    link = re.sub(r"[?&](utm_[^=]+|fbclid|gclid)=[^&#]*", "", link or "")
    return link.rstrip("?&").rstrip("/")


def first_image(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    m = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "") or "")
    return m.group(1) if m else None


def entry_date(entry) -> str:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    return now_iso()


@dataclass
class Item:
    id: str
    source: str
    title: str
    link: str
    published: str
    summary: str
    image: str | None


def fetch_feeds(cfg: dict) -> list[Item]:
    items: list[Item] = []
    max_chars = cfg.get("max_summary_chars", 1500)
    for feed in cfg["feeds"]:
        url = feed["url"]
        if not re.match(r"https?://", url):
            url = str(cfg["_root"] / url)
        parsed = feedparser.parse(url, agent="DailyMotorcycleBot/0.1")
        if parsed.bozo and not parsed.entries:
            print(f"  ! {feed['name']}: {parsed.bozo_exception}", file=sys.stderr)
            continue
        for e in parsed.entries:
            link = canonical_link(e.get("link", ""))
            if not link:
                continue
            body = e.get("summary") or ""
            if e.get("content"):
                body = e["content"][0].get("value", body)
            items.append(
                Item(
                    id=hashlib.sha1(link.encode()).hexdigest(),
                    source=feed["name"],
                    title=clean_text(e.get("title")),
                    link=link,
                    published=entry_date(e),
                    summary=clean_text(body)[:max_chars],
                    image=first_image(e),
                )
            )
        print(f"  {feed['name']}: {len(parsed.entries)} entries")
    return items


def insert_new(con: sqlite3.Connection, items: list[Item]) -> int:
    n = 0
    for it in items:
        cur = con.execute(
            """INSERT OR IGNORE INTO items
               (id, source, title, link, published, summary, image, fetched_at, status)
               VALUES (?,?,?,?,?,?,?,?, 'new')""",
            (it.id, it.source, it.title, it.link, it.published, it.summary, it.image, now_iso()),
        )
        n += cur.rowcount
    con.commit()
    return n


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------

def build_questions(cfg: dict) -> dict:
    qs = {ON_TOPIC: {"type": "noul", "instructions": cfg["on_topic_instructions"].strip()}}
    for c in cfg["categories"]:
        q = {"type": "noul", "instructions": c["instructions"].strip()}
        if c.get("criteria"):
            q["criteria"] = c["criteria"]
        qs[c["slug"]] = q
    return qs


def build_state(row: sqlite3.Row) -> str:
    return f"Source: {row['source']}\nTitle: {row['title']}\n\n{row['summary']}"


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
        self.questions = build_questions(cfg)
        self.name = self.model

    async def classify(self, client: httpx.AsyncClient, state: str) -> tuple[dict, str]:
        body = {"model": self.model, "state": state, "questions": self.questions}
        async with self.sem:
            for attempt in range(6):
                r = await client.post(self.url, json=body, headers=self.headers, timeout=self.timeout)
                if r.status_code in (429, 529, 502, 503):
                    await asyncio.sleep(min(30, 2 ** attempt) + random.random())
                    continue
                r.raise_for_status()
                data = r.json()
                scores = {k: float(v["noul"]) for k, v in data["answers"].items()}
                return scores, data.get("model", self.model)
        raise RuntimeError("Jev: gave up after retries")


class MockClassifier:
    """Keyword stand-in so the pipeline runs without an API key. Not a benchmark."""

    KW = {
        ON_TOPIC: r"motorcycl|moto|bike|rider|motogp|wsbk|supercross|ducati|harley|honda|yamaha|kawasaki|bmw|ktm|triumph|helmet",
        "racing": r"motogp|wsbk|superbike championship|grand prix|race|podium|pole|supercross|motoamerica|isle of man|\btt\b",
        "adventure": r"adventure|\badv\b|dual.sport|overland|off.road|gravel|rally|tenere|africa twin|\bgs\b",
        "electric": r"electric|battery|\bkwh\b|\bev\b|charging|zero motorcycles|livewire|energica",
        "new-models": r"unveil|revealed|reveal|new model|first look|first ride|launch|model year|20\d\d ",
        "industry": r"sales|revenue|earnings|acquir|recall|tariff|regulat|ceo|bankrupt|layoff|market share",
        "custom-vintage": r"custom|cafe racer|restor|vintage|classic|build|scrambler|bobber",
        "gear": r"helmet|jacket|boots|gloves|luggage|intercom|gear review",
    }

    def __init__(self, cfg: dict):
        self.name = "mock"
        self.slugs = [ON_TOPIC] + [c["slug"] for c in cfg["categories"]]

    async def classify(self, client, state: str) -> tuple[dict, str]:
        s = state.lower()
        out = {}
        for slug in self.slugs:
            hits = len(re.findall(self.KW.get(slug, r"$^"), s))
            if slug == ON_TOPIC:
                out[slug] = 0.9 if hits else 0.1
            else:
                out[slug] = round(min(0.97, 0.05 + 0.3 * hits), 3)
        return out, "mock"


def decide(scores: dict, cfg: dict) -> tuple[list, list, str]:
    th = cfg["thresholds"]
    if scores.get(ON_TOPIC, 1.0) < th["on_topic"]:
        return [], [], "offtopic"
    cats = [c["slug"] for c in cfg["categories"]]
    auto = [s for s in cats if scores.get(s, 0) >= th["auto"]]
    pending = [s for s in cats if th["review"] <= scores.get(s, 0) < th["auto"]]
    auto.sort(key=lambda s: -scores[s])          # first = primary category
    pending.sort(key=lambda s: -scores[s])
    if pending or not auto:
        status = "review"                         # uncertain, or nothing confident
    else:
        status = "published"
    return auto, pending, status


async def classify_new(con: sqlite3.Connection, cfg: dict, clf) -> int:
    rows = con.execute("SELECT * FROM items WHERE status='new'").fetchall()
    if not rows:
        return 0
    t0 = time.perf_counter()
    async with httpx.AsyncClient() as client:
        async def one(row):
            try:
                scores, model = await clf.classify(client, build_state(row))
            except Exception as e:  # leave as 'new' so next run retries
                print(f"  ! {row['title'][:60]}: {e}", file=sys.stderr)
                return
            auto, pending, status = decide(scores, cfg)
            con.execute(
                """UPDATE items SET scores=?, on_topic=?, categories=?, pending=?, status=?,
                   classified_by=?, classified_at=? WHERE id=?""",
                (json.dumps(scores), scores.get(ON_TOPIC), json.dumps(auto), json.dumps(pending),
                 status, model, now_iso(), row["id"]),
            )
        await asyncio.gather(*(one(r) for r in rows))
    con.commit()
    dt = time.perf_counter() - t0
    print(f"  classified {len(rows)} items in {dt:.2f}s via {clf.name}")
    return len(rows)


# ----------------------------------------------------------------------------
# Export (what the static site reads)
# ----------------------------------------------------------------------------

def row_to_public(row: sqlite3.Row, labels: dict) -> dict:
    cats = json.loads(row["categories"] or "[]")
    return {
        "id": row["id"],
        "title": row["title"],
        "link": row["link"],
        "source": row["source"],
        "published": row["published"],
        "excerpt": (row["summary"] or "")[:280],
        "image": row["image"],
        "categories": [{"slug": c, "label": labels[c]} for c in cats if c in labels],
        "primary": cats[0] if cats else None,
    }


def export(con: sqlite3.Connection, cfg: dict) -> None:
    out = cfg["_root"] / cfg["output_dir"]
    (out / "categories").mkdir(parents=True, exist_ok=True)
    labels = {c["slug"]: c["label"] for c in cfg["categories"]}

    # Items with at least one confident/approved tag are publishable even if
    # another tag is still pending review.
    rows = con.execute(
        """SELECT * FROM items WHERE status IN ('published','review')
           AND categories IS NOT NULL AND categories != '[]'
           ORDER BY published DESC LIMIT ?""",
        (cfg.get("export_limit", 500),),
    ).fetchall()
    items = [row_to_public(r, labels) for r in rows]
    (out / "items.json").write_text(json.dumps(items, indent=2))

    for slug, label in labels.items():
        sub = [i for i in items if any(c["slug"] == slug for c in i["categories"])]
        (out / "categories" / f"{slug}.json").write_text(
            json.dumps({"slug": slug, "label": label, "items": sub}, indent=2)
        )

    (out / "categories.json").write_text(json.dumps(
        [{"slug": s, "label": l, "count": sum(1 for i in items if any(c["slug"] == s for c in i["categories"]))}
         for s, l in labels.items()], indent=2))

    queue = con.execute(
        "SELECT id, title, source, scores, categories, pending FROM items WHERE status='review' ORDER BY published DESC"
    ).fetchall()
    (out / "review_queue.json").write_text(json.dumps(
        [{"id": q["id"], "title": q["title"], "source": q["source"],
          "applied": json.loads(q["categories"] or "[]"),
          "pending": json.loads(q["pending"] or "[]"),
          "scores": json.loads(q["scores"] or "{}")} for q in queue], indent=2))
    print(f"  exported {len(items)} items, {len(queue)} in review -> {out}/")


# ----------------------------------------------------------------------------
# Review loop (the remaining manual work, now just the uncertain slice)
# ----------------------------------------------------------------------------

def review(con: sqlite3.Connection, cfg: dict) -> None:
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
        # nothing confident and nothing pending: offer the top 3 guesses
        if not cats and not pending:
            pending = sorted(labels, key=lambda s: -scores.get(s, 0))[:3]
        print(f"— {row['title']}  [{row['source']}]")
        print(f"  {row['summary'][:200]}…")
        if cats:
            print(f"  already tagged: {', '.join(labels[c] for c in cats)}")
        skip = False
        for slug in pending:
            ans = input(f"  {labels[slug]} ({scores.get(slug, 0):.2f})? [y/n/s/q] ").strip().lower()
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


def stats(con: sqlite3.Connection, cfg: dict) -> None:
    print("By status:")
    for r in con.execute("SELECT status, COUNT(*) n FROM items GROUP BY status"):
        print(f"  {r['status']:<10} {r['n']}")
    print("By category (auto + approved):")
    counts: dict[str, int] = {}
    for r in con.execute("SELECT categories FROM items WHERE categories IS NOT NULL"):
        for c in json.loads(r["categories"]):
            counts[c] = counts.get(c, 0) + 1
    for c in cfg["categories"]:
        print(f"  {c['label']:<22} {counts.get(c['slug'], 0)}")
    rv = con.execute("SELECT slug, COUNT(*) n, SUM(approved) yes, AVG(score) s FROM reviews GROUP BY slug").fetchall()
    if rv:
        print("Your review decisions (use these to tune thresholds/wording):")
        for r in rv:
            print(f"  {r['slug']:<16} reviewed {r['n']:>3}  approved {r['yes']:>3}  avg score {r['s']:.2f}")


# ----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "review", "export", "stats"])
    ap.add_argument("--mock", action="store_true", help="use keyword stand-in instead of Jev")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    con = db_connect(cfg)

    if args.command == "run":
        print("Fetching feeds…")
        n = insert_new(con, fetch_feeds(cfg))
        print(f"  {n} new items")
        clf = MockClassifier(cfg) if args.mock else JevClassifier(cfg)
        asyncio.run(classify_new(con, cfg, clf))
        export(con, cfg)
    elif args.command == "review":
        review(con, cfg)
    elif args.command == "export":
        export(con, cfg)
    elif args.command == "stats":
        stats(con, cfg)


if __name__ == "__main__":
    main()
