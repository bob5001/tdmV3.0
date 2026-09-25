#!/usr/bin/env python3
"""Interview the owner about how to split a category into children, then test the result with Jev.

  python expand_category.py custom                 stage 1: a text model studies the category's articles and
                                                   asks you targeted questions (saved to out/expand_custom.json)
  python expand_category.py custom --answers a.json  stage 2+3: proposes children from your answers, then Jev
                                                   tests each one on the parent's articles
  python expand_category.py custom --interactive   stages 1-3 in one go, answering at the prompt

Why an interview: the data can show what sub-groups exist, but not which distinctions matter to the
owner (is a restomod Custom or Classic? do builder profiles get their own child?). Wrong children wired
into Jev's schema are costly to unwind, so the owner decides first. Nothing is adopted automatically.

answers.json: [{"id": "q1", "answer": "free text or the chosen option"}, ...]
              plus optional owner-volunteered decisions: {"question": "...", "answer": "..."}
"""
import argparse, asyncio, collections, json, re, sqlite3, sys
from pathlib import Path

import httpx

import dm
from suggest_categories import call_openrouter


def parent_items(con, slug: str, limit: int = 100) -> list:
    rows = con.execute("SELECT * FROM items WHERE status='published' AND lang='en' ORDER BY published DESC").fetchall()
    out = []
    for r in rows:
        if slug in json.loads(r["categories"] or "[]") and json.loads(r["scores"] or "{}").get(slug, 0) >= 0.85:
            out.append(r)
    return out[:limit]


def v1_vocabulary(cfg_cats: list, slug: str) -> dict:
    """How the old WordPress site labelled this territory (counts of v1 category names), if the corpus is here."""
    corpus = dm.ROOT.parent / "backtest" / "corpus.jsonl"
    names = next((c.get("v1", []) for c in yaml_cats() if c["slug"] == slug), [])
    if not corpus.exists() or not names:
        return {}
    cnt = collections.Counter()
    for line in open(corpus, encoding="utf8"):
        r = json.loads(line)
        for c in set(r["v1_job_cats"]) | set(r["v1_hand_cats"]):
            if c in names:
                cnt[c] += 1
    return dict(cnt)


def yaml_cats() -> list:
    import yaml
    p = dm.ROOT.parent / "backtest" / "categories.yaml"
    return yaml.safe_load(p.read_text())["categories"] if p.exists() else []


def article_block(items: list) -> str:
    return "\n".join(f"[{i}] {r['source']}: {r['title']} — {(r['summary'] or '')[:140]}" for i, r in enumerate(items))


def stage1_prompt(cfg, parent, items, vocab) -> str:
    others = "\n".join(f"- {c['slug']}: {c['label']}" for c in cfg["categories"] if c["slug"] != parent["slug"])
    return f"""You are interviewing the owner of a motorcycle news aggregator about how to split ONE category into
child categories. Articles are sorted by a classifier that answers yes/no questions, so every child needs a crisp
definition. Your job now is NOT to propose children yet: it is to find out what the owner wants.

PARENT CATEGORY: {parent['label']} ({parent['slug']}): {parent['instructions']}
OTHER TOP-LEVEL CATEGORIES (children must not duplicate these):
{others}
HOW THE OWNER'S OLD SITE LABELLED THIS TERRITORY (name: article count): {json.dumps(vocab) or 'n/a'}

ARTICLES CURRENTLY IN THE PARENT ({len(items)}):
{article_block(items)}

Do this:
1. Identify the sub-groups that actually appear in these articles (with rough counts and example article numbers).
2. Ask 3-5 questions whose answers would change the design. Each must be grounded in specific evidence from the
   articles above (cite numbers), concern a real ambiguity or trade-off (e.g. a group that could belong to two
   places, a group too small to stand alone, a distinction that may not matter to readers), and offer 2-4
   concrete options. Do not ask anything the data already answers. Do not ask generic taxonomy questions.

Reply with JSON only:
{{"observed_groups":[{{"name":"...","approx_count":0,"examples":[0,1]}}],
  "questions":[{{"id":"q1","question":"...","evidence":"cites article numbers","options":[{{"label":"short","effect":"what it would mean for the design"}}]}}]}}"""


def stage2_prompt(cfg, parent, items, qa) -> str:
    return f"""Design child categories for a motorcycle news aggregator, using the owner's answers.

PARENT: {parent['label']} ({parent['slug']}): {parent['instructions']}
OWNER'S ANSWERS TO MY INTERVIEW QUESTIONS:
{json.dumps(qa, indent=1)}

ARTICLES IN THE PARENT ({len(items)}):
{article_block(items)}

Propose the children the owner's answers point to. Rules:
- Each child needs a slug (kebab-case), label, and `instructions`: ONE sentence starting "The article is about ..."
  that a yes/no classifier can answer, worded to stay inside the parent's territory.
- Respect the answers literally, including "no child for X" and "put X elsewhere".
- Children may overlap (an article can carry several); say so in `notes` when likely.
- Prefer fewer, sharper children. Skip any group the answers say should not exist.

JSON only: {{"children":[{{"slug":"...","label":"...","instructions":"The article is about ...","notes":"..."}}]}}"""


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        sys.exit("model did not return JSON:\n" + text[:500])
    return json.loads(m.group(0))


async def validate_children(cfg, items, children, slug, cut=0.5):
    clf = dm.JevClassifier(cfg)
    qs = {c["slug"]: {"type": "noul", "instructions": c["instructions"]} for c in children}
    sem = asyncio.Semaphore(8)
    scores = {}
    async with httpx.AsyncClient() as client:
        async def one(r):
            async with sem:
                s, _ = await clf.classify(client, dm.build_state(r), qs)
            scores[r["id"]] = s
        await asyncio.gather(*(one(r) for r in items))
    n = len(items)
    covered = {i for i, s in scores.items() if any(v >= cut for v in s.values())}
    report = []
    for c in children:
        hits = [r for r in items if scores[r["id"]][c["slug"]] >= cut]
        strict = sum(scores[r["id"]][c["slug"]] >= 0.85 for r in items)
        multi = [r for r in hits if sum(scores[r["id"]][o["slug"]] >= cut for o in children) > 1]
        report.append({**c, "hits": len(hits), "hits_at_0.85": strict, "share_of_parent": len(hits) / n,
                       "also_in_another_child": len(multi),
                       "samples": [f"{r['source']}: {r['title'][:80]} ({scores[r['id']][c['slug']]:.2f})" for r in hits[:8]]})
    return report, len(covered), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--answers", help="JSON file of answers (skips the interview)")
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--cut", type=float, default=0.5, help="score a child needs (default 0.5: children sit inside a confirmed parent)")
    ap.add_argument("--children", help="JSON file of children to re-test with Jev (no model call), e.g. after tightening wording")
    ap.add_argument("--config", default=str(dm.ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg = dm.load_config(Path(args.config))
    model = cfg["suggest"]["model"]
    parent = next((c for c in cfg["categories"] if c["slug"] == args.slug), None) or sys.exit(f"unknown category {args.slug}")
    con = sqlite3.connect(dm.ROOT / cfg["db_path"]); con.row_factory = sqlite3.Row
    items = parent_items(con, args.slug)
    if len(items) < 20:
        sys.exit(f"only {len(items)} confident items in '{args.slug}'; too few to split")
    out_dir = dm.ROOT / cfg["output_dir"]

    if args.children:
        children = json.loads(Path(args.children).read_text())
        report, covered, n = asyncio.run(validate_children(cfg, items, children, args.slug, args.cut))
        print(f"re-tested {len(children)} children on {n} '{parent['label']}' articles; together they cover {covered}/{n} ({covered/n:.0%})")
        for c in report:
            print(f"\n== {c['label']}  {c['hits']} hits = {c['share_of_parent']:.0%} of parent ({c['hits_at_0.85']} at 0.85), {c['also_in_another_child']} also in another child")
            for smp in c["samples"]:
                print("     -", smp)
        return
    if not args.answers:
        text, meta = call_openrouter(stage1_prompt(cfg, parent, items, v1_vocabulary(cfg["categories"], args.slug)), model)
        s1 = extract_json(text)
        (out_dir / f"expand_{args.slug}.json").write_text(json.dumps(s1, indent=2))
        print(f"{len(items)} articles studied by {meta['model']} (cost ${meta['usage'].get('cost', 0):.3f})\n")
        print("What I see in the data:")
        for g in s1["observed_groups"]:
            print(f"  ~{g['approx_count']:>3}  {g['name']}   e.g. " + " | ".join(items[i]["title"][:45] for i in g["examples"][:2] if i < len(items)))
        if not args.interactive:
            print("\nQuestions (answer in a JSON file and rerun with --answers):")
            for q in s1["questions"]:
                print(f"\n[{q['id']}] {q['question']}\n    evidence: {q['evidence']}")
                for o in q["options"]:
                    print(f"      - {o['label']}: {o['effect']}")
            return
        qa = []
        for q in s1["questions"]:
            print(f"\n[{q['id']}] {q['question']}\n    evidence: {q['evidence']}")
            for k, o in enumerate(q["options"], 1):
                print(f"      {k}. {o['label']}: {o['effect']}")
            a = input("    your answer (number or free text): ").strip()
            qa.append({"question": q["question"], "answer": q["options"][int(a) - 1]["label"] if a.isdigit() and 0 < int(a) <= len(q["options"]) else a})
    else:
        s1 = json.loads((out_dir / f"expand_{args.slug}.json").read_text())
        given = json.loads(Path(args.answers).read_text())
        ans = {a["id"]: a["answer"] for a in given if "id" in a}
        qa = [{"question": q["question"], "answer": ans.get(q["id"], "no preference")} for q in s1["questions"]]
        qa += [a for a in given if "question" in a]           # extra decisions the owner volunteered

    text, meta = call_openrouter(stage2_prompt(cfg, parent, items, qa), model)
    children = extract_json(text)["children"]
    report, covered, n = asyncio.run(validate_children(cfg, items, children, args.slug, args.cut))
    print(f"\n{len(children)} children proposed (model cost ${meta['usage'].get('cost', 0):.3f}); Jev tested them on {n} '{parent['label']}' articles")
    print(f"together they cover {covered}/{n} ({covered/n:.0%}); the rest stay in '{parent['label']}' with no child")
    for c in report:
        flag = "TOO BROAD" if c["share_of_parent"] > 0.6 else ("TOO SMALL" if c["hits"] < 4 else "ok")
        print(f"\n== {c['label']} ({c['slug']}) [{flag}]  {c['hits']} hits = {c['share_of_parent']:.0%} of parent ({c['hits_at_0.85']} at 0.85), {c['also_in_another_child']} also in another child")
        print(f"   {c['instructions']}\n   note: {c.get('notes','')}")
        for s in c["samples"]:
            print("     -", s)
    (out_dir / f"expand_{args.slug}_children.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
