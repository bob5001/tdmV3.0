# Daily Motorcycle — ingest + Jev classifier (v0.2)

Feeds (articles, podcasts, YouTube, Reddit, forums) → normalized JSON → Jev (TypeSafe) classification → JSON files a static site reads.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install feedparser httpx pyyaml
export TYPESAFE_API_KEY=ts_...
```

## Workflow

```bash
python dm.py validate            # 1. check every source; writes out/feed_report.csv
                                 #    paste suggested_url values back into feeds.yaml
python dm.py run --mock --feeds fixtures/feeds.sample.yaml   # offline demo, no key
python dm.py run                 # 2. fetch due feeds, classify new items with Jev, export
python dm.py review              # 3. y/n through uncertain tags only
python dm.py stats               # status / category / per-source yield / your review decisions
```

Run `dm.py run` from cron or launchd every 15 minutes. Each source is polled only as often as its kind allows. Use `--force` to poll everything anyway.

## Files

- `feeds.yaml` is the source registry: kind, ownership, pre-assigned categories, and enabled status. It started from the Gemini survey and is unverified until you run `validate`.
- `config.yaml` holds the Jev settings, thresholds, per-kind defaults, the quality-gate wording, and the categories.

## `validate`

For each source, `validate` fetches the URL and reports one of these:

| Verdict | Meaning |
|---|---|
| `OK` | live feed, with posts per week and the newest date |
| `STALE` | nothing newer than `stale_after_days` |
| `EMPTY` | the feed parses but has no posts |
| `NOT_A_FEED` | an HTML page, and no feed was found on it |
| `FOUND_FEED` | an HTML page where a feed was discovered, via `<link rel=alternate>`, links to .xml/.rss files, or a YouTube @handle resolved to its channel feed |
| `DEAD` | an HTTP error |
| `ERROR` | the request failed |

## Source kinds (`config.yaml → kinds`)

| kind | media | polls every | quality gate |
|---|---|---|---|
| article | article | 30 min | no |
| podcast | podcast (audio URL kept) | 3 h | no |
| video | video (thumbnail kept) | 1 h | no |
| reddit | discussion ("submitted by" boilerplate stripped) | 1 h | **yes** |
| forum | discussion | 2 h | **yes** |
| classifieds | listing | 1 h | no (all disabled; separate product) |

- **Quality gate:** gated sources get one more yes/no question in the same Jev call. Posts below `thresholds.quality` are marked `filtered` and never shown.
- **`assign`:** tags the source's categories without asking Jev. It skips those questions but still runs the on-topic check, which matters for mixed outlets such as Crash.net, where F1 posts would otherwise slip through.
- **`ownership`:** a first-guess label per source. It's exported in `items.json` and `sources.json`, but nothing ranks on it yet.
- **Mock limits:** the `--mock` classifier is plumbing only. It will misjudge things a real model gets right.

## Fetching

- Conditional GET: the ETag and Last-Modified headers are stored per feed, so an unchanged feed costs a 304 and no parsing.
- Per-feed fetch status is kept in the `feeds` table.
- An existing `dm.sqlite` from v0.1 is migrated automatically.

## Output (`out/`)

- `items.json`
- `categories.json` and `categories/<slug>.json`
- `sources.json`: the source directory
- `review_queue.json`
- `feed_report.csv`
