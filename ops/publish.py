#!/usr/bin/env python3
"""TDM publisher: pull feeds, classify with Jev, build, sanity-check, push a data snapshot.

Run every 15 minutes by launchd through ops/launch.sh, from a CLEAN CHECKOUT of origin/main with its own
database (see ops/README.md). That separation is the point: code only ships when you push it to main, and
experiments in your working tree never touch production state.

  python3 ops/publish.py             one cycle: pull -> classify -> build -> checks -> push if changed
  python3 ops/publish.py --dry-run   everything except the commit and push
  python3 ops/publish.py --force     ignore the minimum publish interval

Not an editorial gate: each item is already admitted (or not) by Jev (off-topic, quality, fuzzy tiers, Odd Ball).
What this adds is a HEALTH gate: never publish a broken build, a collapsed item count, or a run where Jev was down.
"""
import argparse, fcntl, hashlib, json, os, shutil, sqlite3, subprocess, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DM = REPO / "TDM-jev-ClaudeProposalCurrent"
SITE = REPO / "site"
HOME = Path(os.environ.get("TDM_PUBLISHER_HOME", REPO.parent))
STATE_DIR = HOME / "state"
GH_REPO = os.environ.get("TDM_GH_REPO", "bob5001/tdmV3.0")

MIN_PUBLISH_MINUTES = 60        # pull every tick, publish at most this often
MIN_ITEMS = 150                 # a snapshot smaller than this is a failure, not a quiet day
MIN_KEEP_FRACTION = 0.5         # ... and so is one that lost half the previous snapshot
MAX_PENDING_FRACTION = 0.10     # items still unclassified after a run (Jev down?) above this: don't publish
PRUNE_DAYS = 180
NOTIFY_AFTER_FAILURES = 3


class Fail(Exception):
    pass


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {msg}", flush=True)


def sh(cmd, cwd, timeout, env=None, check=True):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired:
        raise Fail(f"timeout after {timeout}s: {' '.join(map(str, cmd))}")
    if check and r.returncode != 0:
        tail = "\n".join((r.stdout + "\n" + r.stderr).strip().splitlines()[-12:])
        raise Fail(f"{' '.join(map(str, cmd))} exited {r.returncode}\n{tail}")
    return r


def load_state() -> dict:
    p = STATE_DIR / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "state.json").write_text(json.dumps(st, indent=1))


def notify(title: str, msg: str) -> None:
    """macOS notification, plus Discord if DISCORD_WEBHOOK_URL is set (not required)."""
    try:
        subprocess.run(["osascript", "-e", f'display notification {json.dumps(msg[:200])} with title {json.dumps(title)}'], timeout=10)
    except Exception:
        pass
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"content": f"**{title}**\n{msg[:1500]}"}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log(f"discord notify failed: {e}")


def check_last_deploy(st: dict) -> None:
    """Did Vercel build what we pushed last time? Logged (and notified) on the next tick; never blocks."""
    sha = st.get("last_sha")
    if not sha or not shutil.which("gh"):
        return
    try:
        deps = json.loads(sh(["gh", "api", f"repos/{GH_REPO}/deployments?sha={sha}"], REPO, 30).stdout or "[]")
        if not deps:
            st["deploy_state"] = "none-found"
            return
        sts = json.loads(sh(["gh", "api", f"repos/{GH_REPO}/deployments/{deps[0]['id']}/statuses"], REPO, 30).stdout or "[]")
        state = sts[0]["state"] if sts else "pending"
        if state != st.get("deploy_state") or state in ("failure", "error"):
            log(f"last push {sha[:8]}: Vercel deployment {state}")
        if state in ("failure", "error") and st.get("deploy_notified") != sha:
            notify("TDM: Vercel deploy failed", f"commit {sha[:8]} deployment {state}")
            st["deploy_notified"] = sha
        st["deploy_state"] = state
    except Exception as e:
        log(f"deploy check skipped: {e}")


def config_fingerprint() -> str:
    cfg = yaml.safe_load((DM / "config.yaml").read_text())
    keys = ("categories", "forms", "multi_subject_instructions", "on_topic_instructions", "quality_instructions")
    return hashlib.sha1(json.dumps({k: cfg.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()[:12]


def backup_and_prune(st: dict) -> None:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    db = DM / "dm.sqlite"
    if st.get("backup_day") == today or not db.exists():
        return
    bdir = STATE_DIR / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db)
    dst = sqlite3.connect(bdir / f"dm-{today}.sqlite")
    src.backup(dst)
    dst.close()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PRUNE_DAYS)).isoformat()
    n = src.execute("DELETE FROM items WHERE published < ?", (cutoff,)).rowcount
    src.execute("DELETE FROM story_pairs WHERE a NOT IN (SELECT id FROM items) OR b NOT IN (SELECT id FROM items)")
    src.commit()
    src.close()
    for old in sorted(bdir.glob("dm-*.sqlite"))[:-7]:
        old.unlink()
    st["backup_day"] = today
    log(f"backup written; pruned {n} items older than {PRUNE_DAYS} days")


def snapshot_ids(text: str) -> set:
    try:
        return {i["id"] for i in json.loads(text)}
    except Exception:
        return set()


def cycle(args, st: dict) -> None:
    sh(["git", "config", "user.name", "TDM Publisher"], REPO, 20)
    sh(["git", "config", "user.email", "webmaster@38thweb.com"], REPO, 20)
    check_last_deploy(st)

    # 1. a taxonomy change on main means old items lack scores for the new questions: reclassify them once.
    fp = config_fingerprint()
    db = DM / "dm.sqlite"
    if db.exists() and st.get("fp") and st["fp"] != fp:
        con = sqlite3.connect(db)
        n = con.execute("UPDATE items SET status='new' WHERE lang='en'").rowcount
        con.commit()
        con.close()
        log(f"taxonomy changed ({st['fp']} -> {fp}): reclassifying {n} items")

    # 2. site dependencies, only when needed
    lock = hashlib.sha1((SITE / "package-lock.json").read_bytes()).hexdigest()
    if not (SITE / "node_modules").exists() or st.get("lock") != lock:
        log("npm ci")
        sh(["npm", "ci", "--no-audit", "--no-fund"], SITE, 900)
        st["lock"] = lock

    # 3. pull + classify + group + images + export
    t0 = time.time()
    r = sh([sys.executable, "dm.py", "run"], DM, 1500)
    lines = [l.strip() for l in r.stdout.splitlines() if any(k in l for k in ("new items", "classified", "judged", "page images", "exported"))]
    log(f"pipeline ok in {time.time() - t0:.0f}s: " + " | ".join(lines))

    # health: if Jev was down, items stay 'new' (they retry next tick); publishing then would drop them silently
    con = sqlite3.connect(db)
    pending, total = con.execute("SELECT SUM(status='new'), COUNT(*) FROM items").fetchone()
    con.close()
    pending = pending or 0
    if pending > max(20, MAX_PENDING_FRACTION * total):
        raise Fail(f"{pending}/{total} items still unclassified after the run: Jev or the network is failing; not publishing")

    # 4. build (also validates the site) and refresh site/snapshot from this run's output
    old_items = sh(["git", "show", "HEAD:site/snapshot/items.json"], REPO, 30, check=False).stdout
    sh(["npm", "run", "build"], SITE, 900, env={"TDM_PUBLISH": "1"})
    new_items = (SITE / "snapshot" / "items.json").read_text()
    n_new, n_old = len(json.loads(new_items)), len(json.loads(old_items)) if old_items else 0
    if n_new < MIN_ITEMS or (n_old and n_new < MIN_KEEP_FRACTION * n_old):
        raise Fail(f"snapshot has {n_new} items (previous {n_old}): refusing to publish a collapsed site")
    if not (SITE / "dist" / "index.html").exists():
        raise Fail("build produced no dist/index.html")

    # 5. anything new? meta.json changes every run, so it doesn't count
    changed = sh(["git", "diff", "--quiet", "--", "site/snapshot", ":!site/snapshot/meta.json"], REPO, 30, check=False).returncode != 0
    st["fp"] = fp
    if not changed:
        sh(["git", "checkout", "--", "site/snapshot"], REPO, 30)
        log(f"no change in the snapshot ({n_new} items)")
        return
    added = len(snapshot_ids(new_items) - snapshot_ids(old_items))
    if args.dry_run:
        log(f"DRY RUN: would publish {n_new} items (+{added} new). Not committing.")
        print(sh(["git", "diff", "--stat", "--", "site/snapshot"], REPO, 30).stdout.strip().splitlines()[-1])
        return
    since = (time.time() - st.get("last_publish_ts", 0)) / 60
    if not args.force and since < MIN_PUBLISH_MINUTES:
        sh(["git", "checkout", "--", "site/snapshot"], REPO, 30)
        log(f"changed (+{added} new) but last publish was {since:.0f} min ago (< {MIN_PUBLISH_MINUTES}); waiting")
        return

    # 6. commit + push (data only). Rebase once if you pushed code in the meantime.
    sh(["git", "add", "site/snapshot"], REPO, 30)
    sh(["git", "commit", "-q", "-m", f"Data snapshot {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z ({n_new} items, +{added} new)"], REPO, 30)
    push = sh(["git", "push", "origin", "HEAD:main"], REPO, 120, check=False)
    if push.returncode != 0:
        sh(["git", "pull", "--rebase", "origin", "main"], REPO, 120)
        sh(["git", "push", "origin", "HEAD:main"], REPO, 120)
    sha = sh(["git", "rev-parse", "HEAD"], REPO, 20).stdout.strip()
    st.update(last_sha=sha, last_publish_ts=time.time(), last_publish=datetime.now(timezone.utc).isoformat(), last_items=n_new)
    log(f"PUBLISHED {sha[:8]}: {n_new} items (+{added} new)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lockf = open(STATE_DIR / "lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another cycle is still running; skipping")
        return 0
    st = load_state()
    try:
        cycle(args, st)
        st["failures"] = 0
        st["last_ok"] = datetime.now(timezone.utc).isoformat()
        backup_and_prune(st)
        return_code = 0
    except Exception as e:                      # Fail, or anything unexpected: log it, count it, keep the last good site live
        st["failures"] = st.get("failures", 0) + 1
        log(f"FAILED ({st['failures']} in a row): {e}")
        if st["failures"] in (NOTIFY_AFTER_FAILURES, NOTIFY_AFTER_FAILURES * 4):
            notify("TDM publisher failing", str(e).splitlines()[0])
        return_code = 1
    finally:
        # never leave a half-committed state behind: the launcher hard-resets the clone next tick anyway
        save_state(st)
    return return_code


if __name__ == "__main__":
    sys.exit(main())
