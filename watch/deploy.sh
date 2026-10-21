#!/bin/sh
# nepenthes-watch deploy — run as root ON THE DOCKER HOST.
# Installs the collector + dashboard into /root/nepenthes-tarpit/watch,
# adds a per-minute cron entry, runs the collector once, starts the
# web container, and verifies each step out loud.
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="/root/nepenthes-tarpit/watch"
CRON_LINE="* * * * * /usr/bin/python3 $DEST/collect.py >> $DEST/collect.log 2>&1"
STATS="http://192.168.1.50:8893/stats"
URL="http://192.168.1.50:8894/"

echo "== preflight =="
[ "$(id -u)" = "0" ] || { echo "ERROR: run as root"; exit 1; }
command -v python3 >/dev/null || {
  echo "ERROR: python3 not found on this host. Install with: apt-get install -y python3"
  exit 1
}
command -v docker >/dev/null || { echo "ERROR: docker not found"; exit 1; }
curl -sS -m 10 "$STATS" >/dev/null || {
  echo "ERROR: tarpit stats not reachable at $STATS — is the nepenthes container up?"
  exit 1
}
echo "python3, docker, tarpit stats: OK"

echo "== install to $DEST =="
# Back up the previous install's files WITHOUT moving the directory:
# the web container bind-mounts $DEST/www, and a bind mount pins the
# directory as it was when the container was created — mv-ing the
# folder away leaves the container serving the old copy forever
# (this exact bug froze the dashboard on a stale data.json in v1.2).
if [ -d "$DEST" ]; then
  BAK="${DEST}.bak-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BAK/www"
  for f in collect.py compose.yaml deploy.sh README.md watch.db state.json collect.log; do
    [ -f "$DEST/$f" ] && cp "$DEST/$f" "$BAK/$f"
  done
  [ -f "$DEST/www/index.html" ] && cp "$DEST/www/index.html" "$BAK/www/index.html"
  [ -f "$DEST/www/data.json" ] && cp "$DEST/www/data.json" "$BAK/www/data.json"
  echo "existing install backed up to $BAK"
fi
mkdir -p "$DEST"
cp "$SRC/collect.py" "$DEST/collect.py"
cp "$SRC/compose.yaml" "$DEST/compose.yaml"
mkdir -p "$DEST/www"
cp "$SRC/www/index.html" "$DEST/www/index.html"
# nginx workers run as an unprivileged user — the served files and the
# data.json the collector writes must be world-readable or it's 403s.
chmod -R a+rX "$DEST/www"
chmod +x "$DEST/collect.py"
echo "files installed"

echo "== cron =="
if crontab -l 2>/dev/null | grep -qF "nepenthes-tarpit/watch/collect.py"; then
  echo "collector cron line already present, leaving crontab untouched"
else
  ( crontab -l 2>/dev/null; echo "$CRON_LINE" ) | crontab -
  echo "added: $CRON_LINE"
fi
crontab -l | grep -F "collect.py"

echo "== first collector run =="
/usr/bin/python3 "$DEST/collect.py"
python3 -c "import json; json.load(open('$DEST/www/data.json')); print('data.json valid')"

echo "== web container =="
cd "$DEST" && docker compose up -d --force-recreate
sleep 2
curl -sS -m 10 "$URL" | grep -q "Nepenthes" \
  && echo "dashboard answering at $URL" \
  || { echo "ERROR: dashboard not answering at $URL"; docker logs nepenthes-watch-web --tail 20; exit 1; }
curl -sS -m 10 "${URL}data.json" >/dev/null && echo "data.json serving OK"

echo
echo "DONE. Open the dashboard: $URL  (LAN only)"
echo "Collector log: $DEST/collect.log"
