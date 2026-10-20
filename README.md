# Nepenthes Maze — a self-hosted AI-crawler tarpit

A practical, battle-tested setup guide for running
[Nepenthes](https://zadzmo.org/code/nepenthes/) — the tarpit that
feeds LLM scrapers and aggressive crawlers an endless, slow maze of
Markov-generated nonsense pages — behind Nginx Proxy Manager, on a
throwaway subdomain, with hard resource caps, a real corpus, a
first-crawler phone alert, and lure links that funnel bots from your
other sites into the maze.

This guide is the distilled version of a real homelab deployment,
including the mistakes. Nepenthes itself is upstream's project (see
Credits); this repo is only packaging, configuration, and docs.

## How it works

```
crawler ──> DNS / Cloudflare ──> Nginx Proxy Manager ──> Nepenthes container
                                                        (LAN only, port 8893)
                                                              │
                        stats (LAN only, blocked publicly) ◄──┘
                                                              │
host cron (every 10 min) ── diffs /stats/agents ── new agent? ──> ntfy push
```

Nepenthes serves deterministic pages of ~1.2 KB each, generated from
a Markov chain trained on a text corpus you give it, full of links
that lead deeper into itself. Pages are drip-fed: each one takes
10–65 seconds to deliver. A crawler that wanders in burns *its*
time, threads, and crawl budget — not yours.

## What it costs — measured

From the upstream author's public live-stats example: ~10,000
hits/hour from ~1,850 clients cost him ~14 MB of bandwidth per hour
(~32 kbps) and 1.74% of one CPU core.

From the install this guide is based on:

| State | CPU | Memory (Lua) |
|---|---|---|
| Idle, 33-line starter corpus | 0.06% | ~12.7 MB |
| Idle, 21,951-line corpus (Moby Dick) | 0.06% | ~68.6 MB |
| Upstream example, ~60k-line corpus | ~1.7% under load | ~200 MB |

Nepenthes is single-threaded, so it can never use more than one
core. The compose file caps it at 1 CPU / 512 MB / 128 PIDs anyway —
seatbelts, so even a furious crawler can't hurt the host.

## Before you start — read this part

- **Use a throwaway subdomain** (`bots.example.com`). The tarpit
  hostname should be considered burned for search: engines that
  crawl it may drop it, which is fine because that's its job. Never
  run the maze on a domain you care about.
- **Never set `zero_delay`.** It removes the pacing and turns the
  tarpit into a bandwidth/CPU weapon pointed at your own server.
  Same warning for zero header waits.
- **Never expose the stats endpoints publicly.** `/stats` shows
  who's caught and how it's configured. This setup keeps stats
  LAN-only and makes the proxy return 404 for them.
- **Never port-forward port 8893.** The container binds a LAN IP;
  the only public door is the proxy host you create in step 6.

## Placeholders used in this guide

| Placeholder | Replace with |
|---|---|
| `192.168.1.50` | Your docker host's LAN IP |
| `192.168.1.60` | Your Nginx Proxy Manager host's LAN IP |
| `bots.example.com` | Your throwaway subdomain |
| `ntfy.example.com/nepenthes` | Your ntfy server + topic (or any push URL) |

Search the repo for `192.168.1.50` and `example.com` and replace
every hit with your values before deploying.

## Step 1 — Get the upstream docker bundle

Nepenthes ships as source plus a small official docker bundle.
Download `nepenthes-docker-1.0.tar.gz` from the project's download
page (https://zadzmo.org/downloads/nepenthes/docker) and verify its
checksum against the one published there. (At the time of writing,
the bundle's SHA256 is
`9f988e3014d51026e84c89404de43c0457242a5c2234962e40cd42a359f768c6` —
trust the download page over this README if they ever differ.)

## Step 2 — Lay out the folder

Create the project folder on your docker host, copy this repo's
files into it, and unpack the bundle into `upstream-docker/`:

```
nepenthes-tarpit/
├── docker-compose.yml
├── deploy.sh
├── nepenthes-watch.sh
├── upstream-docker/        <- bundle contents (Dockerfile, compose.yaml, config.yaml)
└── vol/
    ├── config.yml          <- the config the process actually reads
    ├── corpus.txt          <- starter corpus (replace it in step 4)
    ├── seed.txt            <- can be empty
    └── templates/
```

```sh
mkdir nepenthes-tarpit && cd nepenthes-tarpit
tar -xzf /path/to/nepenthes-docker-1.0.tar.gz -C . --strip-components=1
mv nepenthes-docker-1.0 upstream-docker   # if it unpacked as a folder instead
ls upstream-docker   # must show: Dockerfile  compose.yaml  config.yaml
```

## Step 3 — Understand the config before touching it

`vol/config.yml` in this repo is a known-good safe config. Two facts
matter more than everything else in it:

1. **Paths.** The container's start command reads
   `/vol/nepenthes/config.yml`, and the corpus must be at
   `/vol/nepenthes/corpus.txt`. The compose file bind-mounts `./vol`
   over `/vol/nepenthes`, which is why the whole folder is the unit.
   An early version of this setup mounted files under
   `/home/nepenthes/` instead; the container never read them and
   crash-looped with `markov.lua:85: /vol/nepenthes/corpus.txt: No
   such file or directory`. If you see that error, your mount is
   wrong, not your corpus.
2. **No global header waits.** Adding `header_wait_min` /
   `header_wait_max` as global keys makes the 2.8 loader refuse the
   config (`Unknown configuration name`) and crash-loop. Pacing
   lives in the silo: `min_wait` / `max_wait` (this guide uses a
   lazy 10–65 seconds per page).

## Step 4 — Give it a real corpus

The included `corpus.txt` is a 33-line starter — enough to prove it
runs, not enough to be interesting bait. Replace it with a large
plain-text file; public-domain books are perfect. Example with
Moby Dick (Project Gutenberg #2701), stripping the license
header/footer so the babble is all novel:

```sh
cd nepenthes-tarpit
cp vol/corpus.txt vol/corpus-starter.txt.bak
wget -O /tmp/book.txt http://www.gutenberg.org/files/2701/2701-0.txt
awk '/START OF THE PROJECT GUTENBERG EBOOK/{flag=1;next} /END OF THE PROJECT GUTENBERG EBOOK/{flag=0} flag' /tmp/book.txt > vol/corpus.txt
wc -l vol/corpus.txt   # expect tens of thousands of lines
```

The corpus is trained in-memory at every startup, in seconds.
Bigger corpus = more convincing babble = more memory (see the
table above). ~20–60k lines is a good size.

## Step 5 — Deploy and verify locally

```sh
sh deploy.sh
```

Success looks like this, in order:

- container `nepenthes` **Up** (not `Restarting`)
- logs show a startup line for HTTP on port 8893
- `curl http://192.168.1.50:8893/stats` prints JSON
- `curl http://192.168.1.50:8893/maze` returns **HTTP 200, ~1.3 KB,
  after 10–65 seconds**, and the page is full of links back into
  `/maze/…`

A slow maze page is success. A fast one means something is
buffering (see Troubleshooting).

## Step 6 — Open the public door (Nginx Proxy Manager)

Nepenthes stays LAN-only; NPM is the public face.

1. Point `bots.example.com` at your NPM the same way your other
   subdomains reach it (Cloudflare Tunnel hostname, or DNS +
   port-forward — whatever your existing hosts use).
2. NPM → **Proxy Hosts → Add Proxy Host**:
   - Domain Names: `bots.example.com`
   - Scheme: `http`
   - Forward Hostname/IP: `192.168.1.50`
   - Forward Port: `8893`
   - Cache Assets: **off** · Block Common Exploits: **off** ·
     Websockets: **off**
3. **SSL tab**: request/select a certificate, **Force SSL on**.
4. **Advanced tab** → *Custom Nginx Configuration* — paste exactly:

   ```nginx
   proxy_buffering off;
   proxy_request_buffering off;
   proxy_set_header X-Forwarded-For $remote_addr;
   proxy_set_header X-Real-IP $remote_addr;
   proxy_read_timeout 120s;
   proxy_send_timeout 120s;
   location = /stats { return 404; }
   location /stats/ { return 404; }
   ```

   `proxy_buffering off` matters: without it the proxy swallows the
   slow drip-feed and hands the crawler a fast page — no tarpit.
   The two `location` blocks are what keep stats private while the
   maze is public.
5. Save, then verify **from outside your LAN** (phone on cellular
   is ideal):
   - `https://bots.example.com/stats` → **404**
   - `https://bots.example.com/maze` → loads, slowly, full of
     strange prose and links

## Step 7 — Get told when the first crawler lands

Stats only remember the last hour (`stats_remember_time: 3600`),
and they're LAN-only, so nothing outside the host can watch them
meaningfully. `nepenthes-watch.sh` runs on the host instead: every
10 minutes it diffs `/stats/agents` against a permanent seen-list
and pushes new user-agents to an ntfy topic.

```sh
# edit STATS_URL, STATE, NTFY_URL at the top of the script first
chmod +x nepenthes-watch.sh
./nepenthes-watch.sh          # first run: seeds the list silently (your browser is in it)
./nepenthes-watch.sh --test   # sends a test push - confirm it reaches your phone
(crontab -l 2>/dev/null; echo '*/10 * * * * /root/nepenthes-tarpit/nepenthes-watch.sh') | crontab -
crontab -l | grep nepenthes   # verify the entry actually stuck
```

From then on, the first stranger in the maze buzzes your phone
within 10 minutes — GPTBot, ClaudeBot, a random scanner, whoever
shows up first.

## Step 8 — Feed it: lure links on your other sites

The subdomain gets discovered on its own (new hostnames leak via
certificate-transparency logs, and scanners enumerate wordlists —
`bots` is in all of them). Lure links speed that up and,
defensively, redirect crawler attention from your real pages into
the maze.

**Static HTML** (any site you edit directly, e.g. a Cloudflare
Pages site): paste the snippet in
[`examples/lure-snippet.html`](examples/lure-snippet.html) just
before `</body>` in a page. It must be in the raw HTML, not added
by JavaScript — scrapers read raw responses.

**Packaged apps you can't edit** (Nextcloud, Authentik, …): let NPM
inject it instead — the block in
[`examples/npm-subfilter.txt`](examples/npm-subfilter.txt) goes in
that proxy host's Advanced tab.

**Where NOT to put a lure:** any page you want ranked in search.
A hidden outbound link is a classic deceptive-SEO signal, and the
risk lands on the *source* site. Login portals, dashboards, app
subdomains: ideal. Public docs and marketing pages: never. One link
per domain is plenty.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Crash-loop: `markov.lua:85: /vol/nepenthes/corpus.txt: No such file or directory` | Config/corpus not under the `/vol/nepenthes` mount | Use the single `./vol:/vol/nepenthes` bind mount from this repo |
| Crash-loop: `Unknown configuration name: header_wait_min` | Global header-wait keys in the config | Delete them; pacing is per-silo `min_wait`/`max_wait` |
| Maze page returns instantly with 200 | Proxy is buffering the drip-feed | `proxy_buffering off;` in the NPM Advanced paste; also make sure you're testing through the proxy host, not a cache |
| `/stats` answers publicly | Missing `location` blocks | Add both `/stats` blocks to the Advanced config and save |
| Public tests fail with 403 from a VPS/datacenter IP, but work from a browser | Your CDN/bot filter blocks datacenter IPs — working as designed | Test from a normal connection (phone browser) instead |
| Container up, stats fine, nobody ever visits | Normal at first | Subdomain discovery takes hours–days (CT logs, scanners); lure links (step 8) speed it up |
| `time: not found` in deploy output on minimal systems | `time` isn't a shell builtin everywhere | The included `deploy.sh` uses curl's own `-w` timing instead |

## Security notes

- The tarpit is deliberately a honeypot on a **dedicated
  subdomain** with **no real content** behind it. Keep it that way:
  don't proxy anything real under the same hostname.
- Stats are your visitor log — that's why they're LAN-only and
  404 in public.
- The lure is the only cross-site piece. Everything else is
  contained: one container, LAN-bound port, hard caps.
- Nepenthes cannot tell a search engine from an LLM scraper, and
  neither can this setup. That's the accepted trade — the tarpit
  domain is a sacrifice zone, and your real domains stay out of it.

## Files in this repo

| File | What it is |
|---|---|
| `docker-compose.yml` | The capped, LAN-bound container definition |
| `vol/config.yml` | Known-good safe Nepenthes 2.8 config |
| `vol/corpus.txt` | 33-line starter corpus (replace it — step 4) |
| `deploy.sh` | Deploy + end-to-end LAN verification |
| `nepenthes-watch.sh` | First-crawler ntfy alerter (step 7) |
| `examples/lure-snippet.html` | Hidden lure link for static sites |
| `examples/npm-subfilter.txt` | NPM injection variant for packaged apps |

## Credits

- **Nepenthes** is by zadzmo — https://zadzmo.org/code/nepenthes/ —
  including the docker bundle this setup is built on. This repo is
  an unofficial setup guide and is not affiliated with the project.
- Corpus example uses *Moby Dick* by Herman Melville, via
  [Project Gutenberg](https://www.gutenberg.org/), public domain.

## License

The guide text, scripts, and configuration in this repository are
released under the MIT License (see `LICENSE`). Nepenthes itself is
distributed under its own license by its author; Project Gutenberg
texts are public domain.
