# Publisher (ops/)

Keeps the live site fresh without anyone at the keyboard.

```
launchd (every 15 min) -> launch.sh -> git reset --hard origin/main -> publish.py
   pull feeds -> classify (Jev) -> group stories -> images -> export -> npm run build
   -> health checks -> commit site/snapshot -> git push origin main -> Vercel builds
```

## Why a separate clone

`~/Projects/tdm-publisher/repo` is its own clone with its own `dm.sqlite`. It is hard-reset to `origin/main` every
tick, so:

- **code ships only when you push it** (half-finished edits in your working tree, or another session's, never publish);
- **experiments never touch production state** (a mass re-classify in your dev tree does not touch the publisher's DB);
- a taxonomy change you push (categories, forms, wording) is detected by fingerprint and all English items are
  re-classified once, automatically.

The bot owns exactly one thing in git: `site/snapshot/`. Dev builds write to `site/src/data` (gitignored).

## Health gate (not an editorial gate)

Each item is already admitted by Jev (off-topic and quality gates, fuzzy tiers, Odd Ball). The publisher only refuses to
publish when: the pipeline or build fails; more than 10% of items are still unclassified (Jev down); the snapshot has
fewer than 150 items or lost half of the previous one. It then keeps the last good site live and retries next tick.

Pull runs every 15 minutes; a change is published at most once an hour (`MIN_PUBLISH_MINUTES`).

## Files (outside the repo, in `~/Projects/tdm-publisher/`)

| path | what |
|---|---|
| `repo/` | the clean clone (its `TDM-jev-ClaudeProposalCurrent/.env` holds only `TYPESAFE_API_KEY`) |
| `launch.sh` | copy of `ops/launch.sh` |
| `state/state.json` | last publish, failure count, config fingerprint |
| `state/backups/` | daily copy of `dm.sqlite`, last 7 kept |
| `~/Library/Logs/tdm-publisher/` | `publisher.log` |
| `~/Library/LaunchAgents/com.tdm.publisher.plist` | the schedule |

## Operating it

```bash
~/Projects/tdm-publisher/launch.sh --dry-run          # everything except commit/push
~/Projects/tdm-publisher/launch.sh --force            # publish now, ignoring the hourly limit
tail -f ~/Library/Logs/tdm-publisher/publisher.log

launchctl unload ~/Library/LaunchAgents/com.tdm.publisher.plist   # stop
launchctl load   ~/Library/LaunchAgents/com.tdm.publisher.plist   # start
```

Failures write to the log; 3 in a row raise a macOS notification, and so does a failed Vercel deployment of the last
push. Set `DISCORD_WEBHOOK_URL` in the publisher's `.env` to also post there (not required).

## Known limits (deliberate for the MVP)

- Each data push adds a snapshot commit (about 1 MB gzipped, less after git deltas). Fine for now; the upgrade path is
  uploading the snapshot to object storage and triggering a Vercel deploy hook, so data never enters git history.
- Runs on this Mac only. `dm.sqlite` (the only copy of the scores) is backed up daily to `state/backups/`.
- No item-level blocklist yet: to pull an item, remove its source in `feeds.yaml` or wait for the classifier to drop it.
