#!/bin/bash
# Regenerate status.json and push to the public tracker repo (GitHub Pages).
# Cron: */10 * * * *  — runs as spencer. Pushes ONLY this public repo.
set -u
cd /home/spencer/canvass-tracker || exit 1
LOCK=/tmp/canvass-tracker-push.lock
exec 9>"$LOCK"; flock -n 9 || exit 0   # skip if a prior run is still going

python3 tools/gen_status.py >> tools/gen.log 2>&1

# Commit only if status.json changed
if ! git diff --quiet status.json 2>/dev/null; then
  git add status.json
  git -c user.name="canvass-bot" -c user.email="tracker@canvasslaw.com" \
      commit -q -m "status $(date +%H:%M)" 2>>tools/gen.log
  git push -q origin main 2>>tools/gen.log && echo "$(date) pushed" >> tools/gen.log
fi
