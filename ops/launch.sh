#!/bin/bash
# Stable launcher for the publisher. Lives OUTSIDE the clone in real use (~/Projects/tdm-publisher/launch.sh);
# this copy is the reference. It never runs from a working tree you edit: it hard-resets its own clone to
# origin/main each tick, so code ships only when it has been pushed, then hands over to ops/publish.py.
set -euo pipefail
export PATH=/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.11/bin:/usr/local/bin:/usr/bin:/bin
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=20"
cd "$(dirname "$0")/repo"
git fetch -q origin
git reset -q --hard origin/main
exec python3 ops/publish.py "$@"
