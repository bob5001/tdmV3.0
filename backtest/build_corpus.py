"""Turn the raw v1 export (corpus_raw.jsonl) into a clean backtest corpus.

Ground truth idea: v1 feed jobs stamped categories automatically (by source), so those tags say
nothing about content. Tags a human added on top (post cats minus the job's cats) are the only
real labels we have. They are positives only: a missing tag does not mean "not that category".
"""
import html, json, re, random, collections

CATS = {1:"uncategorized",8:"featured-post",10:"oems",11:"classic-bikes",12:"sport-bikes",13:"adventure",
 14:"tips-and-tricks",16:"reviews",17:"uk",18:"france",19:"gear",20:"historical",21:"motocross",22:"racing",
 24:"for-sale",25:"news",26:"custom",27:"touring",28:"from-the-forums",29:"best-of",30:"cruisers",31:"electric",
 32:"cafe-racers",33:"choppers",34:"bobbers",35:"baggers",40:"unfiltered",42:"mx",45:"how-to",
 46:"motorcycles-in-movies-and-tv",47:"women",48:"odd-ducks",49:"restoration",50:"swedish-language",
 51:"french-language",52:"bike-shows",53:"scramblers"}

STYLE = re.compile(r"<style.*?</style>|<script.*?</script>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
CSS = re.compile(r"[.#][\w\-\s.#>,:]*\{[^}]*\}")
SRC = re.compile(r"^\s*Source:\s*[^\n]*?(?:-|\n)", re.I)

def clean(s):
    s = STYLE.sub(" ", s or "")
    s = TAG.sub(" ", s)
    s = html.unescape(s)
    s = CSS.sub(" ", s)
    s = re.sub(r"\[\[\{.*?\}\]\]", " ", s, flags=re.S)
    return re.sub(r"\s+", " ", s).strip()

rows = []
for line in open("corpus_raw.jsonl", encoding="utf8"):
    d = json.loads(line)
    ids = [int(x) for x in (d["cat_ids"] or "").split(",") if x]
    job = {int(t.split("_")[1]) for t in (d["job_terms"] or "").split(",") if t}
    text = clean(d["content"])
    text = re.sub(r"^Source:\s*\S+(\s\S+){0,3}?\s", "", text)  # drop the "Source: X" lead-in
    rows.append({
        "id": d["id"], "title": d["title"], "source": d["source"], "date": d["date"][:10], "url": d["url"],
        "text": text[:1500], "text_len": len(text),
        "v1_job_cats": sorted(CATS[i] for i in ids if i in job),
        "v1_hand_cats": sorted(CATS[i] for i in ids if i not in job),
    })

with open("corpus.jsonl", "w", encoding="utf8") as f:
    for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")

hand = collections.Counter(c for r in rows for c in r["v1_hand_cats"])
short = sum(1 for r in rows if r["text_len"] < 80)
nosrc = sum(1 for r in rows if not r["source"])
print("posts", len(rows), "| empty/short text (<80 chars):", short, "| no feed job:", nosrc)
print("hand-tag counts:", dict(hand.most_common(12)))
print("posts with any hand tag:", sum(1 for r in rows if r["v1_hand_cats"]))
r = next(r for r in rows if "electric" in r["v1_hand_cats"])
print(json.dumps({k: r[k] for k in ("title","source","v1_job_cats","v1_hand_cats")}), "\n", r["text"][:300])
