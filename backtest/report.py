#!/usr/bin/env python3
"""Score cached Jev results against v1 hand tags. Positives only: an untagged post is 'unknown', not 'negative'."""
import json, sys, collections
rows = [json.loads(l) for l in open("results.jsonl")]
latest = rows[-1]["wording"]
rows = [r for r in rows if r["wording"] == latest]
cats = sorted({k for r in rows for k in r["scores"] if not k.startswith("_")})
pos = [r for r in rows if r["truth"]]
unk = [r for r in rows if not r["truth"]]
print(f"wording {latest}: {len(pos)} hand-tagged, {len(unk)} untagged (unknown truth)\n")

print(f"{'category':<12}{'n':>4} {'recall@.50':>11} {'recall@.85':>11} {'med score':>10} | {'untagged hit@.85':>17}")
for c in cats:
    p = [r for r in pos if c in r["truth"]]
    if not p:
        continue
    s = sorted(r["scores"][c] for r in p)
    r50 = sum(x >= .5 for x in s) / len(s); r85 = sum(x >= .85 for x in s) / len(s)
    hit = sum(r["scores"][c] >= .85 for r in unk) / len(unk)
    print(f"{c:<12}{len(p):>4} {r50:>11.0%} {r85:>11.0%} {s[len(s)//2]:>10.2f} | {hit:>17.1%}")

off = [r for r in pos if r["scores"]["_on_topic"] < .6]
print(f"\nhand-tagged posts Jev calls off-topic (<0.60): {len(off)}/{len(pos)}  (v1 tag error, or Jev error?)")
for r in off[:8]: print("  -", r["title"][:80], "|", r["source"], "| tagged", r["truth"])

cnt = collections.Counter(sum(v >= .85 for k, v in r["scores"].items() if not k.startswith("_")) for r in unk)
print("\ncategories >=0.85 per untagged post:", dict(sorted(cnt.items())))
none = [r for r in unk if r["scores"]["_on_topic"] >= .6 and not any(v >= .5 for k, v in r["scores"].items() if not k.startswith("_"))]
print(f"on-topic untagged posts with NO category >=0.50: {len(none)}/{len(unk)}  (the gap: generic news/reviews/gear)")
for r in none[:6]: print("  -", r["title"][:80], "|", r["source"])

if len(sys.argv) > 1:
    c = sys.argv[1]
    print(f"\n--- '{c}' hand-tagged, Jev score < 0.5 ---")
    for r in sorted((r for r in pos if c in r["truth"] and r["scores"][c] < .5), key=lambda r: r["scores"][c])[:12]:
        print(f"  {r['scores'][c]:.2f} on_topic={r['scores']['_on_topic']:.2f} | {r['title'][:75]} | {r['source']}")
