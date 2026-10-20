#!/bin/sh
# Nepenthes tarpit - deploy + verify. Run ON the docker host, from
# the folder containing docker-compose.yml:
#   sh deploy.sh
# The verify steps at the end ARE the test: a healthy install shows
# stats JSON and a SLOW maze page. Slow is success here.
#
# >>> EDIT: replace 192.168.1.50 with your docker host's LAN IP <<<
# >>> (must match the bind IP in docker-compose.yml).          <<<
set -eu
cd "$(dirname "$0")"

HOST_IP="192.168.1.50"

echo "== 1. Upstream docker bundle present? =="
if [ ! -f ./upstream-docker/Dockerfile ]; then
  echo "MISSING: ./upstream-docker/Dockerfile - unpack the bundle first (README step 2)."
  exit 1
fi
ls -la ./upstream-docker

echo "== 2. Local files present and sane? =="
ls -la ./vol/config.yml ./vol/corpus.txt ./docker-compose.yml
wc -l ./vol/corpus.txt
touch ./vol/seed.txt
test -f ./vol/seed.txt && echo "seed.txt OK"
grep -q "corpus: '/vol/nepenthes/corpus.txt'" ./vol/config.yml && echo "corpus path OK" || { echo "ABORT: corpus path wrong in vol/config.yml"; exit 1; }
grep -q '^[[:space:]]*zero_delay[[:space:]]*:' ./vol/config.yml && { echo "ABORT: zero_delay is SET - remove it"; exit 1; } || echo "zero_delay not set OK"

echo "== 3. Compose config validates? =="
docker compose config

echo "== 4. Build + up =="
docker compose up -d --build

echo "== 5. Container state (give it 10-20s - the corpus trains at startup) =="
sleep 15
docker compose ps
docker stats --no-stream nepenthes || true
echo "--- last 50 log lines ---"
docker compose logs --tail=50 nepenthes

echo "== 6. LAN checks (from the docker host) =="
echo "Stats endpoint (LAN only - it must NOT be reachable publicly):"
curl -sS "http://${HOST_IP}:8893/stats" || echo "(stats not answering - check logs above)"
echo
echo "Maze page - 10-65 seconds is SUCCESS, not a hang:"
curl -sS -o /tmp/nepenthes-test.html -w "HTTP %{http_code}  %{size_download} bytes  %{time_total}s\n" \
  "http://${HOST_IP}:8893/maze" || echo "(maze not answering - check logs above)"
echo "Links in that page (should be many, all pointing back into /maze):"
grep -o 'href="[^"]*"' /tmp/nepenthes-test.html | head -20 || true

echo "== DONE =="
