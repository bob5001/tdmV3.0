#!/usr/bin/env python3
"""Suggest new categories from the Odd Ball pile, then test each suggestion before a human sees it.

  python suggest_categories.py            propose (OpenRouter model) + validate (Jev), print a report
  python suggest_categories.py --dry      just print the prompt

Stage 1: a text model sees the current categories and the items nothing fit, and proposes categories.
         (Jev cannot do this: it answers typed questions, it does not write.)
Stage 2: every proposal is turned into a Jev yes/no question and asked of ALL published English items.
         A proposal is only worth a human's time if it catches leftovers without swallowing everything
         (the 'culture' category matched 45% of the site: a catch-all in disguise) and without
         duplicating an existing category.
Nothing is adopted automatically. Approved ones go into backtest/categories.yaml, then sync_config.py.
"""
import argparse, asyncio, json, os, re, sqlite3, sys
from pathlib import Path

import httpx

import dm  # noqa: E402  (loads .env, gives us JevClassifier and config loading)


def propose(cfg: dict, orphans: list, model: str, min_items: int) -> tuple[list, dict]:
    cats = "\n".join(f"- {c['slug']}: {c['instructions']}" for c in cfg["categories"])
    items = "\n".join(f"[{i}] {r['source']}: {r['title']} — {(r['summary'] or '')[:160]}" for i, r in enumerate(orphans))
    prompt = f"""You help curate a motorcycle news site. Articles are auto-sorted into categories by a classifier
that answers yes/no questions. These are the CURRENT categories:

{cats}

The articles below are on-topic but fit NONE of them. Propose new categories that would absorb clusters of
these leftovers. Rules:
- Each proposal must be supported by at least {min_items} of the articles below (list their numbers).
- A category must be a coherent subject a reader would browse, not a vague bucket ("culture", "misc", "other").
- It must be clearly different from the current categories.
- `instructions` must be one sentence starting "The article is about ..." that a classifier can answer yes/no.
- Propose fewer rather than weaker categories. Zero is a fine answer.

Reply with JSON only: {{"proposals":[{{"slug":"kebab-case","label":"Title Case","instructions":"The article is about ...","members":[3,7,...],"rationale":"one line"}}]}}

ARTICLES:
{items}"""
    return prompt


def call_openrouter(prompt: str, model: str) -> tuple[str, dict]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY not set (put it in .env)")
    r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=180,
                   headers={"Authorization": f"Bearer {key}"},
                   json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3})
    r.raise_for_status()
    d = r.json()
    return d["choices"][0]["message"]["content"], {"model": d.get("model"), "usage": d.get("usage")}


async def validate(cfg: dict, con, proposals: list) -> list:
    rows = con.execute("SELECT id, title, summary, source, media, categories FROM items WHERE status='published' AND lang='en'").fetchall()
    clf = dm.JevClassifier(cfg)
    qs = {p["slug"]: {"type": "noul", "instructions": p["instructions"]} for p in proposals}
    sem = asyncio.Semaphore(8)
    results = {}
    async with httpx.AsyncClient() as client:
        async def one(r):
            async with sem:
                scores, _ = await clf.classify(client, dm.build_state(r), qs)
            results[r["id"]] = scores
        await asyncio.gather(*(one(r) for r in rows))
    orphan_ids = {r["id"] for r in rows if not json.loads(r["categories"] or "[]")}
    out = []
    for p in proposals:
        hits = [r for r in rows if results[r["id"]][p["slug"]] >= 0.5]
        in_orphans = [r for r in hits if r["id"] in orphan_ids]
        out.append({**p, "hits": len(hits), "share_of_site": len(hits) / len(rows),
                    "orphans_caught": len(in_orphans), "orphans_total": len(orphan_ids),
                    "already_categorised": len(hits) - len(in_orphans),
                    "samples": [f"{r['source']}: {r['title'][:80]}" for r in hits[:8]]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--config", default=str(dm.ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg = dm.load_config(Path(args.config))
    sg = cfg.get("suggest", {})
    con = sqlite3.connect(dm.ROOT / cfg["db_path"]); con.row_factory = sqlite3.Row
    orphans = [r for r in con.execute("SELECT * FROM items WHERE status='published' AND lang='en' ORDER BY published DESC")
               if not json.loads(r["categories"] or "[]")][: sg.get("max_orphans", 150)]
    if len(orphans) < sg.get("min_orphans", 20):
        sys.exit(f"only {len(orphans)} Odd Ball items; wait until there are more to learn from")
    prompt = propose(cfg, orphans, sg["model"], sg.get("min_items", 6))
    if args.dry:
        print(prompt); return
    text, meta = call_openrouter(prompt, sg["model"])
    m = re.search(r"\{.*\}", text, re.S)
    proposals = json.loads(m.group(0))["proposals"] if m else []
    print(f"{len(orphans)} Odd Ball items -> {len(proposals)} proposals from {meta['model']} (usage {meta['usage']})")
    if not proposals:
        return
    for p in proposals:
        p["supported_by"] = [orphans[i]["title"][:70] for i in p.get("members", []) if 0 <= i < len(orphans)][:6]
    report = asyncio.run(validate(cfg, con, proposals))
    for p in report:
        # Articles can carry several categories, so overlap with existing ones is expected, not a flaw.
        # Judge on breadth (catch-all in disguise?) and on how many leftovers it actually rescues.
        if p["share_of_site"] > sg.get("max_share", 0.12):
            verdict = "TOO BROAD"
        elif p["orphans_caught"] < sg.get("min_items", 6):
            verdict = "WEAK: rescues few leftovers"
        else:
            verdict = "worth a look"
        print(f"\n== {p['label']} ({p['slug']})  [{verdict}]\n   {p['instructions']}\n   why: {p['rationale']}")
        print(f"   Jev hits: {p['hits']} = {p['share_of_site']:.1%} of site | catches {p['orphans_caught']}/{p['orphans_total']} Odd Ball items | overlaps {p['already_categorised']} already-categorised")
        for s in p["samples"][:6]:
            print("     -", s)
    (dm.ROOT / cfg["output_dir"] / "category_suggestions.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
