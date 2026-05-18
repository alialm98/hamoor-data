#!/usr/bin/env bash
# Run update_prices.py and push any new data to the public GitHub repo so the
# iOS app's 15-minute poll picks up fresh closes.
#
# Designed to be invoked by launchd (no terminal) — keeps stderr separate so
# launchd surfaces real failures in `data/logs/update.err.log`.
#
# Quiet by design when there's nothing to commit (weekends, mid-day reruns).

set -euo pipefail

cd "$(dirname "$0")"

# 1. Fetch + merge prices into both stores.
/usr/bin/python3 update_prices.py

# 2. Stage just the generated outputs — never script/source changes.
git add output/

# 3. Commit only if something actually changed (avoid empty commits).
if git diff --cached --quiet; then
    echo "[$(date '+%F %T')] No data changes to push."
    exit 0
fi

stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
git commit -m "Daily price update $stamp"

# 4. Push. Will use the credentials stored in ~/.config/gh (gh CLI
#    installs a git credential helper), so no token needs to live in this file.
git push origin main

echo "[$(date '+%F %T')] Pushed fresh prices to origin/main."
