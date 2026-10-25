# CDN — Content Delivery Network in pure Python

A working Content Delivery Network built with only the Python standard library.
Every component is a separate process bound to its own IP address, and clients
are routed to the nearest edge by a real GeoDNS server based on their source IP.

## Architecture

```
                      ┌──────────────────────────┐
  client 127.1.2.10 ─►│ GeoDNS  127.0.0.2:1053   │  "cdn.local A?"  → 127.0.0.12 (nearest healthy edge)
   (Paris)            └──────────────────────────┘
        │
        ▼  HTTP (Host: cdn.local)
  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌──────────────┐
  │ us-east    │ │ eu-west    │ │ ap-south   │ │ ap-northeast │   Edge caches (LRU + TTL)
  │ 127.0.0.11 │ │ 127.0.0.12 │ │ 127.0.0.13 │ │ 127.0.0.14   │
  └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └──────┬───────┘
        └──────────────┴──── cache miss ─────────────┘
                              ▼
                   ┌──────────────────────┐
                   │ Origin 127.0.0.100   │   master copy of content
                   └──────────────────────┘
```

| Component | Address | Role |
|---|---|---|
| GeoDNS | `127.0.0.2:1053/udp` | Authoritative DNS for `cdn.local`. Maps the query's source IP to a location (GeoIP table) and answers with the nearest **healthy** edge. Health-checks edges every 2 s. |
| Edges | `127.0.0.11–14:8080` | Byte-bounded LRU cache with TTL from the origin's `Cache-Control`. Adds `X-Cache: HIT/MISS` and `X-Edge` headers. Endpoints: `/_health`, `/_stats`, `POST /_purge?path=`. |
| Origin | `127.0.0.100:8080` | Serves `origin_content/` (auto-generated) with a simulated 200 ms long-haul latency. |
| Clients | `127.1.1.x` Boston, `127.1.2.x` Paris, `127.1.3.x` Bangalore, `127.1.4.x` Seoul | Each city has its own subnet. |

All addresses live in [`config.json`](config.json).

## Requirements

- Python 3.8+
- Linux (the whole `127.0.0.0/8` range is loopback, so no root or network setup
  is needed). On macOS, add aliases first: `sudo ifconfig lo0 alias 127.0.0.2` etc.

## Usage

```bash
python3 cdn.py demo                 # start everything and run a scripted demo
python3 cdn.py start                # start everything and keep running (Ctrl+C to stop)

python3 cdn.py client 127.1.4.10 /index.html   # fetch as a Seoul client
python3 cdn.py stats                           # cache stats from every edge
python3 cdn.py purge /index.html               # purge from every edge (or 'all')
```

With standard tools while `start` is running:

```bash
dig @127.0.0.2 -p 1053 cdn.local -b 127.1.2.10       # Paris → 127.0.0.12 (eu-west)
dig @127.0.0.2 -p 1053 cdn.local -b 127.1.4.10       # Seoul → 127.0.0.14 (ap-northeast)
curl -i --interface 127.1.2.10 -H "Host: cdn.local" http://127.0.0.12:8080/index.html
```

## What the demo shows

1. **Geo-routing** — each city's client IP is resolved to its nearest edge.
2. **Caching** — first request is a MISS (~205 ms via origin), repeats are HITs (~1–5 ms).
3. **Shared cache** — different client IPs hitting the same edge share cached objects.
4. **LRU eviction** — the 64 KB edge cache evicts least-recently-used objects.
5. **Purge** — invalidating an object forces a re-fetch from origin.
6. **404 passthrough** — origin errors are relayed to the client.
7. **Failover** — the eu-west process is killed; GeoDNS detects it and sends Paris to us-east.

Per-process logs are written to `logs/<component>.log`.

## Deploying on real machines

Put real IPs in `config.json`, copy the same file to every machine, and run one role per host:

```bash
python3 cdn.py origin          # on the origin machine
python3 cdn.py edge eu-west    # on each edge machine (use its name)
python3 cdn.py dns             # on the DNS machine
```

Update the `geoip` table with your clients' real subnets and open UDP 1053 and TCP 8080.
