#!/usr/bin/env python3
"""Run backtest posts through Jev (TypeSafe System One) and cache the scores.

  python run_jev.py --limit 3            smoke test
  python run_jev.py --neg 300            all hand-tagged positives + 300 random untagged posts
  python report.py                       score the cached results

Results go to results.jsonl, keyed by post id + a hash of the question wording, so re-running only
pays for posts/wordings not scored yet. The API key comes from TYPESAFE_API_KEY (env or ../TDM-jev-ClaudeProposalCurrent/.env).
"""
import argparse, asyncio, hashlib, json, os, random, sys
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE.parent / "TDM-jev-ClaudeProposalCurrent" / ".env"
BASE_URL = "https://api.typesafe.ai"
MODEL = "jev-latest"


def load_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("TYPESAFE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        sys.exit("TYPESAFE_API_KEY not found in env or .env")
    return key


def build_questions(cfg: dict) -> dict:
    qs = {"_on_topic": {"type": "noul", "instructions": cfg["on_topic"].strip()}}
    for c in cfg["categories"]:
        qs[c["slug"]] = {"type": "noul", "instructions": c["instructions"].strip()}
    return qs


def build_state(p: dict) -> str:
    return f"Article from {p['source'] or 'unknown'}\nTitle: {p['title']}\n\n{p['text'][:1500]}".strip()


def wording_hash(qs: dict) -> str:
    return hashlib.sha1(json.dumps(qs, sort_keys=True).encode()).hexdigest()[:10]


async def classify(client, sem, key, state, qs):
    body = {"model": MODEL, "state": state, "questions": qs}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    async with sem:
        for attempt in range(6):
            r = await client.post(f"{BASE_URL}/v1/systemone", json=body, headers=headers, timeout=30)
            if r.status_code in (429, 502, 503, 529):
                await asyncio.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            return data, {k: float(v["noul"]) for k, v in data["answers"].items()}
    raise RuntimeError("gave up after retries")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only score this many posts (smoke test)")
    ap.add_argument("--neg", type=int, default=300, help="random untagged posts to add as (probable) negatives")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--categories", default=str(HERE / "categories.yaml"))
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.categories).read_text())
    qs = build_questions(cfg)
    wh = wording_hash(qs)
    v1_to_slug = {v: c["slug"] for c in cfg["categories"] for v in c["v1"]}

    posts = [json.loads(l) for l in open(HERE / "corpus.jsonl", encoding="utf8")]
    for p in posts:
        p["truth"] = sorted({v1_to_slug[c] for c in p["v1_hand_cats"] if c in v1_to_slug})
    pos = [p for p in posts if p["truth"]]
    untagged = [p for p in posts if not p["v1_hand_cats"]]
    random.Random(args.seed).shuffle(untagged)
    sample = pos + untagged[: args.neg]
    if args.limit:
        sample = sample[: args.limit] if args.limit > 3 else [pos[0], pos[len(pos) // 2], untagged[0]][: args.limit]

    done = set()
    out_path = HERE / "results.jsonl"
    if out_path.exists():
        done = {(r["id"], r["wording"]) for r in map(json.loads, open(out_path))}
    todo = [p for p in sample if (p["id"], wh) not in done]
    print(f"{len(pos)} positives, {len(sample)} in sample, {len(todo)} to score (wording {wh})")

    key, sem = load_key(), asyncio.Semaphore(8)
    async with httpx.AsyncClient() as client:
        async def one(p):
            try:
                raw, scores = await classify(client, sem, key, build_state(p), qs)
            except Exception as e:
                print(f"  ! {p['id']}: {e}", file=sys.stderr)
                return None
            return {"id": p["id"], "wording": wh, "title": p["title"], "source": p["source"],
                    "truth": p["truth"], "scores": scores, "model": raw.get("model")}
        results = [r for r in await asyncio.gather(*(one(p) for p in todo)) if r]

    with open(out_path, "a", encoding="utf8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"scored {len(results)}/{len(todo)}")
    if args.limit and results:
        for r in results:
            top = sorted(r["scores"].items(), key=lambda kv: -kv[1])[:4]
            print(f"- {r['title'][:70]} | truth={r['truth']} | model={r['model']} | " +
                  ", ".join(f"{k}={v:.2f}" for k, v in top))


asyncio.run(main())
