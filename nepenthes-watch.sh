#!/bin/sh
# nepenthes-watch.sh - get a phone push when a NEW crawler finds the maze.
#
# Why this exists: Nepenthes stats only remember the last hour
# (stats_remember_time), and the stats endpoint is LAN-only on
# purpose. A once-a-day check would miss almost everything. This
# script runs ON the docker host every 10 minutes, diffs the current
# /stats/agents against a permanent seen-list, and pushes any new
# user-agent to an ntfy topic.
#
# First run just seeds the seen-list (no alert - it will contain
# your own browser). `--test` sends a test push.
#
# >>> EDIT the three values below before installing. <<<
set -u

STATS_URL="http://192.168.1.50:8893/stats/agents"
STATE="/root/nepenthes-tarpit/known-agents.txt"
NTFY_URL="https://ntfy.example.com/nepenthes"

if [ "${1:-}" = "--test" ]; then
  curl -sS -m 10 -H "Title: Nepenthes watcher test" \
    -d "Watcher installed on the docker host. New crawlers in the maze will land here." \
    "$NTFY_URL"
  echo
  exit 0
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# If stats are unreachable or invalid, do nothing (never corrupt state).
curl -sS -m 10 "$STATS_URL" | jq -r 'keys[]' 2>/dev/null | sort -u > "$TMP" || exit 0
[ -s "$TMP" ] || exit 0

# First run: seed the baseline (includes your own browser).
if [ ! -f "$STATE" ]; then
  cp "$TMP" "$STATE"
  exit 0
fi

NEW="$(comm -13 "$STATE" "$TMP")"
if [ -n "$NEW" ]; then
  { cat "$STATE"; cat "$TMP"; } | sort -u > "$STATE.new" && mv "$STATE.new" "$STATE"
  curl -sS -m 10 -H "Title: Nepenthes: crawler in the maze" \
    -d "New agent(s) in the maze:
$NEW" \
    "$NTFY_URL" >/dev/null || true
fi
exit 0
