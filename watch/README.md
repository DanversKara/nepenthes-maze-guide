# Nepenthes Watch — tarpit dashboard

A live + historical dashboard for the Nepenthes tarpit at bots.example.com.
Nepenthes itself only exposes bare JSON stats and forgets everything
after an hour; this adds the missing screen.

## What you get

- **Live**: requests served, tarpit memory/uptime, who's in the maze in
  the last hour (agents + IPs), requests-per-minute chart.
- **History**: every agent and IP ever seen, with first seen / last seen
  / peak reported hits — kept forever, not just Nepenthes' 1-hour window.
- **What they saw**: per-visitor page trails — the actual sequence of
  /maze pages each crawler walked through, stitched from the container
  logs.

## How it works

```
cron (every minute) ──> collect.py ──┬──> watch.db   (SQLite archive)
                                     └──> www/data.json
nepenthes container logs ──(tail)────┘          │
                                     nginx container :8894 serves www/
```

- `collect.py` (stdlib-only Python) polls `http://192.168.1.50:8893/stats`,
  `/stats/agents`, `/stats/addresses`, archives sightings to SQLite, tails
  `docker logs nepenthes` for request lines, and rewrites `data.json`.
  Stats polls are excluded from trails, so the trail shows real maze
  traffic only. If the tarpit is unreachable it exits without touching
  existing data.
- `www/index.html` is one static file — no build step. It fetches
  `data.json` every 30 s and renders everything client-side.
- The existing `nepenthes-watch.sh` ntfy alerter is **untouched** — it
  keeps doing its job off the same stats endpoints.

## Install (docker host, as root)

Already summarized in the delivery message; the short version:

```
unzip nepenthes-watch-v1.zip
cd nepenthes-watch
./deploy.sh
```

`deploy.sh` checks python3/docker/tarpit reachability first, backs up
any previous install to `watch.bak-<timestamp>` (history DB carried
over), installs the cron line, runs the collector once, starts the web
container, and verifies the dashboard answers. It prints what it did at
every step.

Dashboard: **http://192.168.1.50:8894/** (LAN only).

## Remote viewing (optional)

The page displays visitor IPs and user-agents. It's bound to the LAN
only and 8894 is **not** forwarded on pfSense. If you want it on your
phone away from home, prefer your WireGuard. If you do publish it, use
a fresh NPM proxy host (e.g. `watch.example.com` → `192.168.1.50:8894`)
and put an NPM access list or basic auth in front of it.

## Trails note

Trail extraction is best-effort against the container's log lines: any
line carrying a path is stored verbatim, with IP / path / agent pulled
out where found. If bots have visited but the trails section stays
empty, the log format differs from expectations — grab a sample and the
parser can be tuned exactly:

```
docker logs nepenthes --since 1h 2>&1 | tail -20
```

To disable log tailing entirely (stats + history only), add
`NEPENTHES_TAIL_LOGS=0` to the cron line's environment.

## Knobs (environment variables for collect.py)

- `NEPENTHES_BASE` — stats base URL (default `http://192.168.1.50:8893`)
- `NEPENTHES_CONTAINER` — container to tail (default `nepenthes`)
- `NEPENTHES_TAIL_LOGS` — `1`/`0` (default `1`)

## Data

- `watch.db` — SQLite. Sightings and hits are kept indefinitely; raw
  stats snapshots prune after 30 days. Growth is a few MB/month.
- `collect.log` — one line per run, in the install folder.

## Rollback

```
crontab -e        # delete the collect.py line
cd /root/nepenthes-tarpit/watch && docker compose down
rm -rf /root/nepenthes-tarpit/watch
# or restore the backup: mv /root/nepenthes-tarpit/watch.bak-<ts> /root/nepenthes-tarpit/watch
```
