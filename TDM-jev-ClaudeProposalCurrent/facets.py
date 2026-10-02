"""Test the brand and event facets (facets.yaml; the live logic is in dm.py) against Jev on real items.

Brands are found in two steps so Jev is only asked about brands that are actually there:
  mention  deterministic alias match on title + summary
  subject  Jev yes/no, asked only for the mentioned brands
Events are one Jev yes/no question each, asked for every English item (like forms).
Whether brand coverage is racing or road comes from the item's own Racing category, not another question.

  python facets.py test [N]     ask Jev on the N most recent published items (default 120) and report
                                (FACETS_DB=path to test on another database, e.g. a copy of the publisher's)
"""
import asyncio, json, os, re, sqlite3, sys
from pathlib import Path

import httpx

import dm



def load() -> tuple[list, list]:
    cfg = dm.load_config(dm.ROOT / "config.yaml")
    return cfg["_brands"], cfg["_events"]


def mentions(text: str, brands: list) -> list:
    return dm.brand_mentions(text, brands)


async def test(n: int) -> None:
    cfg = dm.load_config(dm.ROOT / "config.yaml")
    brands, events = load()
    con = sqlite3.connect(os.environ.get("FACETS_DB") or dm.ROOT / cfg["db_path"])   # FACETS_DB: e.g. a copy of the publisher DB
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM items WHERE status='published' AND lang='en' ORDER BY published DESC LIMIT ?", (n,)).fetchall()
    clf = dm.JevClassifier(cfg)
    results = []
    async with httpx.AsyncClient() as client:
        async def one(r):
            found = mentions(f"{r['title']} {r['summary'] or ''}", brands)
            qs = dm.facet_questions(cfg, r)
            scores, _ = await clf.classify(client, dm.build_state(r), qs)
            results.append({"id": r["id"], "title": r["title"], "source": r["source"],
                            "racing": "racing" in json.loads(r["categories"] or "[]"),
                            "mentioned": [b["slug"] for b in found], "scores": scores})
        await asyncio.gather(*(one(r) for r in rows))
    out = dm.ROOT / cfg["output_dir"] / "facets_test.json"
    out.write_text(json.dumps(results, indent=2))

    cut = 0.5
    name = {b["slug"]: b["name"] for b in brands}
    print(f"{len(results)} items. Brand mentions -> Jev says 'substantially about' (cut {cut}); racing share of subjects")
    tally = {}
    for r in results:
        for s in r["mentioned"]:
            t = tally.setdefault(s, [0, 0, 0])
            t[0] += 1
            if r["scores"].get(f"brand-{s}", 0) >= cut:
                t[1] += 1
                t[2] += r["racing"]
    for s, (m, sub, rac) in sorted(tally.items(), key=lambda kv: -kv[1][0]):
        print(f"  {name[s]:<16} mentioned {m:>3}  subject {sub:>3}  (racing {rac})")
    print("\nEvents (items scoring >= 0.5, and >= 0.85):")
    for e in events:
        k = f"event-{e['slug']}"
        hits = sorted((r for r in results if r["scores"].get(k, 0) >= cut), key=lambda r: -r["scores"][k])
        print(f"\n== {e['label']}: {len(hits)} ({sum(r['scores'][k] >= 0.85 for r in hits)} confident)")
        for r in hits[:6]:
            print(f"     {r['scores'][k]:.2f}  {r['source']}: {r['title'][:90]}")
    print(f"\nfull results: {out}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        asyncio.run(test(int(sys.argv[2]) if len(sys.argv) > 2 else 120))
    else:
        print(__doc__)
