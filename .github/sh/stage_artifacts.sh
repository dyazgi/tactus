#!/bin/bash
# Runs on hpc-batch. Stages the small CI artifacts into $3:
#   <case>/ci_status.txt, <case>/checks/summary.*   from each casedir listed in $1
#   failed_tasks/<suite>/<family>/<task>.<try>      last 512K of each "<file><TAB><name>" in $2
set -uo pipefail
casedirs=$1
failed=$2
dest=$3
rm -rf "$dest"
mkdir -p "$dest"

while IFS= read -r d; do
  [ -d "$d" ] || continue
  rsync -am --include='ci_status.txt' --include='checks/' --include='checks/summary.*' \
    --exclude='*' "$d"/ "$dest/$(basename "$d")/"
done < "$casedirs"

[ -f "$failed" ] || exit 0
while IFS=$'\t' read -r src rel; do
  [ -f "$src" ] || continue
  mkdir -p "$dest/failed_tasks/$(dirname "$rel")"
  tail -c 512K "$src" > "$dest/failed_tasks/$rel"
done < "$failed"
