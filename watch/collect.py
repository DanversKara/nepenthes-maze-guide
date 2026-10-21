#!/usr/bin/env python3
"""Nepenthes tarpit watch collector.

Runs on the docker host (the tarpit's /stats endpoints are LAN-only).
Each run:
  1. Polls /stats, /stats/agents, /stats/addresses and archives them to
     SQLite — Nepenthes itself only remembers the last hour, this keeps
     the full history (first seen / last seen per agent and address).
  2. Tails the nepenthes container's logs (best effort) to record the
     actual page trail each visitor crawled ("what they saw").
  3. Regenerates www/data.json for the static dashboard page.

Stdlib only. Driven by cron, one run per minute. Exits non-zero only
when the tarpit itself is unreachable, and never touches existing
data in that case.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("NEPENTHES_BASE", "http://192.168.1.50:8893").rstrip("/")
CONTAINER = os.environ.get("NEPENTHES_CONTAINER", "nepenthes")
TAIL_LOGS = os.environ.get("NEPENTHES_TAIL_LOGS", "1") == "1"
HOST_IP = os.environ.get("NEPENTHES_HOST_IP", "192.168.1.50")
REMEMBER_S = 3600  # nepenthes stats_remember_time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "watch.db")
STATE_PATH = os.path.join(HERE, "state.json")
WWW_DIR = os.path.join(HERE, "www")
DATA_JSON = os.path.join(WWW_DIR, "data.json")

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PATH = re.compile(r"(/[^\s\"'<>\[\]]+)")
QUOTED = re.compile(r'"([^"]{4,300})"')
DOCKER_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def fetch(path: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        BASE + path, headers={"User-Agent": "nepenthes-watch-collector/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def find_number(obj, names, depth=0):
    """First numeric value whose key contains any of `names` (nested ok)."""
    if depth > 3 or not isinstance(obj, dict):
        return None
    for key, val in obj.items():
        lk = str(key).lower()
        if any(n in lk for n in names) and isinstance(val, (int, float)):
            return val
    for val in obj.values():
        found = find_number(val, names, depth + 1)
        if found is not None:
            return found
    return None


def normalize_pairs(payload) -> list[tuple[str, int | None]]:
    """Accept {key: count}, {key: {count: n}}, or [{agent:.., count:..}]."""
    pairs: list[tuple[str, int | None]] = []
    if isinstance(payload, dict):
        items = payload.items()
    elif isinstance(payload, list):
        items = []
        for entry in payload:
            if isinstance(entry, str):
                items.append((entry, None))
            elif isinstance(entry, dict):
                key = None
                for cand in ("agent", "user_agent", "ua", "address", "ip", "key", "name"):
                    if entry.get(cand):
                        key = str(entry[cand])
                        break
                if key:
                    items.append((key, entry))
    else:
        return pairs
    for key, val in items:
        count = None
        if isinstance(val, (int, float)):
            count = int(val)
        elif isinstance(val, dict):
            for cand in ("count", "hits", "requests", "visits", "total"):
                if isinstance(val.get(cand), (int, float)):
                    count = int(val[cand])
                    break
        pairs.append((str(key)[:500], count))
    return pairs


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, raw TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sightings(
            id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
            kind TEXT NOT NULL, key TEXT NOT NULL, count INTEGER);
        CREATE INDEX IF NOT EXISTS sightings_kind_key
            ON sightings(kind, key, ts);
        CREATE TABLE IF NOT EXISTS hits(
            id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
            ip TEXT, agent TEXT, path TEXT, raw TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS hits_dedupe ON hits(ts, raw);
        """
    )


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def extract_agent(line: str) -> str | None:
    segments = QUOTED.findall(line)
    for seg in segments:
        low = seg.lower()
        if any(t in low for t in ("mozilla", "bot", "spider", "crawler",
                                  "compatible", "curl", "python", "go-http")):
            return seg[:300]
    return None


def tail_logs(db: sqlite3.Connection, state: dict, now: int) -> None:
    """Best effort: pull new container log lines, keep the request trail.

    Nepenthes' exact log format is not assumed — every line that carries
    a path is stored verbatim in `raw`, with ip/path/agent extracted
    where they can be found. Stats polls (ours + the ntfy watcher's)
    are excluded so the trail shows real maze traffic only.
    """
    since = int(state.get("log_watermark", now - 3600))
    try:
        proc = subprocess.run(
            ["docker", "logs", "--timestamps", "--since", str(since), CONTAINER],
            capture_output=True, text=True, timeout=60,
        )
        lines = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # docker missing, container renamed, ...
        print(f"log tail skipped: {exc}", file=sys.stderr)
        return
    inserted = 0
    max_ts = since
    for line in lines.splitlines():
        line = line.strip()
        if not line:
            continue
        ts = now
        m = DOCKER_TS.match(line)
        if m:
            try:
                ts = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")))
                # docker --timestamps are UTC; mktime is local. Convert via
                # calendar to be safe.
                import calendar
                ts = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                ts = now
        max_ts = max(max_ts, ts)
        paths = PATH.findall(line)
        # Drop HTTP version fragments ("/1.1") and stats polls (ours +
        # the ntfy watcher's) — the trail is real maze traffic only.
        paths = [p for p in paths
                 if not re.fullmatch(r"/\d+(\.\d+)?", p)
                 and not p.startswith("/stats")]
        if not paths:
            continue  # startup notices, stats polls, etc.
        path = next((p for p in paths if p.startswith("/maze")), paths[0])
        ips = [ip for ip in IPV4.findall(line) if ip != "0.0.0.0"]
        ip = ips[0] if ips else None
        try:
            db.execute(
                "INSERT OR IGNORE INTO hits(ts, ip, agent, path, raw)"
                " VALUES (?, ?, ?, ?, ?)",
                (ts, ip, extract_agent(line), path[:300], line[:1000]),
            )
            inserted += db.execute("SELECT changes()").fetchone()[0]
        except sqlite3.Error:
            pass
    state["log_watermark"] = max(max_ts, now)
    if inserted:
        print(f"log tail: {inserted} new hit lines")


def grouped(db, kind, since_ts=None):
    where = "kind = ?"
    args: list = [kind]
    if since_ts is not None:
        where += " AND ts >= ?"
        args.append(since_ts)
    rows = db.execute(
        f"""SELECT key, MIN(ts), MAX(ts), COUNT(*), MAX(count),
                   (SELECT count FROM sightings s2
                     WHERE s2.kind = s.kind AND s2.key = s.key
                     ORDER BY s2.ts DESC LIMIT 1)
            FROM sightings s WHERE {where}
            GROUP BY key ORDER BY MAX(ts) DESC LIMIT 300""",
        args,
    ).fetchall()
    return [
        {"key": k, "first_seen": mn, "last_seen": mx, "polls": n,
         "max_count": mc, "last_count": lc}
        for (k, mn, mx, n, mc, lc) in rows
    ]


def main() -> int:
    now = int(time.time())
    try:
        stats_raw = fetch("/stats")
        stats = json.loads(stats_raw)
    except Exception as exc:
        print(f"collector: cannot reach {BASE}/stats: {exc}", file=sys.stderr)
        return 1
    payloads: dict[str, list[tuple[str, int | None]]] = {}
    for kind, path in (("agent", "/stats/agents"), ("address", "/stats/addresses")):
        try:
            pairs = normalize_pairs(json.loads(fetch(path)))
        except Exception as exc:
            print(f"collector: {path} failed ({exc}); continuing", file=sys.stderr)
            pairs = []
        if kind == "address":
            # Nepenthes keys addresses by the whole forwarding chain
            # ("client, last-hop") — the first entry is the real visitor.
            pairs = [(key.split(",")[0].strip(), count) for key, count in pairs]
        payloads[kind] = pairs

    os.makedirs(WWW_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    try:
        init_db(db)
        db.execute("INSERT INTO snapshots(ts, raw) VALUES (?, ?)", (now, stats_raw))
        for kind, pairs in payloads.items():
            db.executemany(
                "INSERT INTO sightings(ts, kind, key, count) VALUES (?, ?, ?, ?)",
                [(now, kind, key, count) for key, count in pairs],
            )
        state = load_state()
        if TAIL_LOGS:
            tail_logs(db, state, now)
            save_state(state)
        db.execute("DELETE FROM snapshots WHERE ts < ?", (now - 30 * 86400,))
        db.commit()

        mem = find_number(stats, ["memory_usage", "memory", "rss"])
        data = {
            "generated_at": now,
            "base": BASE,
            "live": {
                "fetched_at": now,
                "stats": stats,
                "memory_mb": round(mem / 1048576, 1) if mem else None,
                "uptime_s": find_number(stats, ["uptime"]),
                "requests_total": find_number(
                    stats, ["total_requests", "requests", "request_count",
                            "served", "hits"]),
            },
            "active": {
                "window_s": REMEMBER_S,
                "agents": grouped(db, "agent", now - REMEMBER_S),
                "addresses": grouped(db, "address", now - REMEMBER_S),
            },
            "history": {
                "agents": grouped(db, "agent"),
                "addresses": grouped(db, "address"),
            },
            "activity": [
                {"ts": ts, "requests_total":
                    find_number(json.loads(raw),
                                ["total_requests", "requests", "request_count",
                                 "served", "hits"])}
                for ts, raw in db.execute(
                    "SELECT ts, raw FROM snapshots ORDER BY ts DESC LIMIT 180"
                ).fetchall()
            ][::-1],
            "hits": [
                {"ts": ts, "ip": ip, "agent": agent, "path": path}
                for ts, ip, agent, path in db.execute(
                    "SELECT ts, ip, agent, path FROM hits"
                    " ORDER BY ts DESC LIMIT 400"
                ).fetchall()
            ],
            "totals": {
                "agents_all_time": db.execute(
                    "SELECT COUNT(DISTINCT key) FROM sightings"
                    " WHERE kind = 'agent'").fetchone()[0],
                "addresses_all_time": db.execute(
                    "SELECT COUNT(DISTINCT key) FROM sightings"
                    " WHERE kind = 'address'").fetchone()[0],
                "hits_recorded": db.execute(
                    "SELECT COUNT(*) FROM hits").fetchone()[0],
                "first_poll": db.execute(
                    "SELECT MIN(ts) FROM snapshots").fetchone()[0],
            },
        }
    finally:
        db.close()

    tmp = DATA_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, DATA_JSON)
    print(
        f"collector ok: {len(payloads['agent'])} agents, "
        f"{len(payloads['address'])} addresses in the current window"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
