#!/usr/bin/env bash
# Commit and push the generated files named in $FILES, if they changed.
#
# The build takes minutes, and Nina's Mac pushes sales.html/index.html from its
# launchd jobs whenever it wakes up — run #36 (16 Sep) was rejected that way.
# So commit onto whatever main is NOW: reset --mixed keeps the freshly generated
# files in the working tree while moving HEAD to the latest remote commit, and
# only $FILES are ever staged, so a concurrent push of index.html (or anything
# else) survives untouched.
set -u
git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
for attempt in 1 2 3; do
  git fetch origin main
  git reset --mixed FETCH_HEAD
  git add $FILES
  if git diff --cached --quiet; then
    echo "$FILES unchanged — nothing to commit"
    exit 0
  fi
  git commit -m "${FILES%.html} $(TZ=Asia/Jakarta date '+%Y-%m-%d %H:%M') WIB"
  if git push origin HEAD:main; then
    exit 0
  fi
  echo "::warning::push rejected (someone else pushed first) — retry $attempt/3"
  sleep 5
done
echo "::error::could not push $FILES after 3 attempts"
exit 1
