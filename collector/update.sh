#!/bin/sh
# Monthly top-up: fetch this year's new rulings, re-parse, rebuild rulings.db.
# Then commit data/ and push: Render rebuilds rulings.db and redeploys.
set -e
cd "$(dirname "$0")/.."
YEAR=$(( $(date +%Y) + 543 ))
python3 collector/rd_rulings.py crawl --years "$YEAR" "$((YEAR - 1))"
python3 collector/rd_rulings.py parse
python3 collector/build_db.py --report
python3 collector/embed.py          # only new/changed chunks hit the API
