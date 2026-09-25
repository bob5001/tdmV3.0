# Daily Motorcycle — Jev classifier prototype

RSS feeds → normalized JSON → Jev (TypeSafe) classification → JSON files a static site (Astro/React) reads.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install feedparser httpx pyyaml
export TYPESAFE_API_KEY=ts_...        # from TypeSafe (or use OpenRouter; see config.yaml)
```

## Use

```bash
python dm.py run --mock   # full pipeline with a keyword stand-in, no key needed
python dm.py run          # real run: fetch, classify only NEW items with Jev, export
python dm.py review       # y/n through the uncertain tags only
python dm.py stats        # counts + how your review decisions compare to Jev's scores
```

Run `dm.py run` from cron or launchd every 15–30 minutes, then rebuild and deploy the static site.

## How classification works

- Each item is one API call: the title and cleaned summary as `state`, plus one **Noul** (yes/no) question per category and an `_on_topic` gate question. Jev answers them all in parallel, so a post can get more than one tag (electric + adventure, for example).
- Thresholds in `config.yaml`:
  - `auto` (0.85): the tag is applied automatically.
  - `review` (0.50): the item goes to the review queue.
  - `on_topic` (0.60): below this, the item is hidden (car or e-bike posts from mixed feeds).
- An item with at least one confident tag is published right away. Any uncertain extra tags wait in the queue.
- Every review decision is saved in the `reviews` table. Use it to adjust category wording or thresholds per category.
- Items are deduped by canonical link (tracking params stripped), so each story is classified and billed only once. Failed API calls stay `new` and are retried on the next run.

## Output (`out/`)

| File | Contents |
|---|---|
| `items.json` | newest N publishable items with categories (the `primary` field is the highest-scoring category) |
| `categories.json` | category list with counts |
| `categories/<slug>.json` | items for each category page |
| `review_queue.json` | uncertain items with every score |

## Before going live

1. Put the real Feedzy feed URLs into `config.yaml` and match the categories to the site's current ones.
2. Backtest: export roughly 200 posts you've already categorized by hand from WordPress, run them through Jev, and compare. That shows where to set the thresholds before you trust auto-tagging.
3. The mock classifier only tests the plumbing. It says nothing about Jev's accuracy.
