#!/usr/bin/env python3
"""
Content Delivery Network (CDN) - multi-process, multi-IP, real DNS routing.
Pure Python standard library.

Every component is a separate OS process bound to its own IP address
(see config.json). On one Linux box the whole 127.0.0.0/8 range is
loopback, so no root or network setup is needed. To deploy on real
machines, put their real IPs in config.json and run one role per machine.

  GeoDNS server   127.0.0.2:1053 (UDP)  Authoritative DNS for cdn.local.
                                        Looks up the *source IP* of the query
                                        in the GeoIP table and answers with the
                                        A record of the nearest healthy edge.
  Edge servers    127.0.0.11-14:8080    Regional caches (LRU + TTL). On a miss
                                        they fetch from the origin.
  Origin server   127.0.0.100:8080      Master copy of all content.
  Clients         127.1.X.Y             Each city has its own subnet, so the
                                        CDN sees a different client IP.

Usage:
  python3 cdn.py demo                  start everything, run scripted demo
  python3 cdn.py start                 start everything, keep running
  python3 cdn.py origin                run only the origin   (one per machine)
  python3 cdn.py edge <name>           run only one edge
  python3 cdn.py dns                   run only the GeoDNS server
  python3 cdn.py client <src-ip> <path>   fetch like a real client
  python3 cdn.py stats                 show cache stats from every edge
  python3 cdn.py purge <path|all>      purge from every edge

Try with standard tools while 'start' is running:
  dig @127.0.0.2 -p 1053 cdn.local -b 127.1.2.10     # Paris client -> eu-west IP
  dig @127.0.0.2 -p 1053 cdn.local -b 127.1.4.10     # Seoul client -> ap-northeast IP
  curl -i --interface 127.1.2.10 -H "Host: cdn.local" http://127.0.0.12:8080/index.html
"""

import http.client
import ipaddress
import json
import math
import os
import random
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CDN_CONFIG", os.path.join(BASE_DIR, "config.json"))
CONTENT_DIR = os.path.join(BASE_DIR, "origin_content")
LOG_DIR = os.path.join(BASE_DIR, "logs")

MIME = {
    ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
    ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg",
    ".txt": "text/plain",
}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


CFG = load_config()


def log(component, msg):
    print(f"[{time.strftime('%H:%M:%S')}] {component:<13} {msg}", flush=True)


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points on Earth."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_edge(name):
    for e in CFG["edges"]:
        if e["name"] == name:
            return e
    sys.exit(f"unknown edge '{name}'. Known: {[e['name'] for e in CFG['edges']]}")


def create_sample_content():
    """Populate the origin with a few files if it is empty."""
    os.makedirs(os.path.join(CONTENT_DIR, "images"), exist_ok=True)
    os.makedirs(os.path.join(CONTENT_DIR, "static"), exist_ok=True)
    samples = {
        "index.html": b"<html><body><h1>Hello from the CDN origin</h1></body></html>\n",
        "static/app.js": b"console.log('app loaded');\n" * 200,
        "static/style.css": b"body { font-family: sans-serif; }\n" * 100,
        "images/logo.png": os.urandom(20 * 1024),
        "images/banner.jpg": os.urandom(30 * 1024),
        "images/hero.jpg": os.urandom(40 * 1024),
    }
    for rel, data in samples.items():
        path = os.path.join(CONTENT_DIR, rel)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)


def send_json(handler, obj, code=200):
    body = json.dumps(obj, indent=2).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def serve_http(ip, port, handler, name):
    srv = ThreadingHTTPServer((ip, port), handler)
    srv.daemon_threads = True
    log(name, f"listening on http://{ip}:{port}  (pid {os.getpid()})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------- #
# Origin server
# --------------------------------------------------------------------------- #

def run_origin():
    create_sample_content()
    cfg = CFG["origin"]
    latency = cfg.get("latency_ms", 0) / 1000
    counter = {"n": 0}
    lock = threading.Lock()

    class OriginHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(latency)  # origin is "far away" from every user
            path = urllib.parse.urlparse(self.path).path
            if path == "/_stats":
                return send_json(self, {"origin_requests": counter["n"]})
            with lock:
                counter["n"] += 1
            if path == "/":
                path = "/index.html"
            full = os.path.normpath(os.path.join(CONTENT_DIR, path.lstrip("/")))
            if not full.startswith(CONTENT_DIR + os.sep) or not os.path.isfile(full):
                self.send_error(404, "Not found on origin")
                return

            with open(full, "rb") as f:
                body = f.read()
            ext = os.path.splitext(full)[1]
            # HTML changes often -> short TTL; static assets -> longer TTL.
            max_age = 10 if ext == ".html" else 60

            self.send_response(200)
            self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", f"public, max-age={max_age}")
            self.send_header("X-Served-By", "origin")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            edge = self.headers.get("X-CDN-Edge", "direct")
            log("origin", f"{self.client_address[0]:<15} ({edge}) {fmt % args}")

    serve_http(cfg["ip"], cfg["port"], OriginHandler, "origin")


# --------------------------------------------------------------------------- #
# Edge server
# --------------------------------------------------------------------------- #

class LRUCache:
    """Byte-bounded LRU cache with per-entry TTL. Thread-safe."""

    def __init__(self, capacity_bytes):
        self.capacity = capacity_bytes
        self.size = 0
        self.items = OrderedDict()   # key -> (body, headers, expires_at)
        self.lock = threading.Lock()
        self.hits = self.misses = self.evictions = self.expired = 0

    def get(self, key):
        with self.lock:
            entry = self.items.get(key)
            if entry is None:
                self.misses += 1
                return None
            if time.time() >= entry[2]:
                self._remove(key)
                self.expired += 1
                self.misses += 1
                return None
            self.items.move_to_end(key)
            self.hits += 1
            return entry

    def put(self, key, body, headers, ttl):
        if len(body) > self.capacity:
            return  # too large to cache at all
        with self.lock:
            if key in self.items:
                self._remove(key)
            while self.size + len(body) > self.capacity:
                _, (old_body, _, _) = self.items.popitem(last=False)
                self.size -= len(old_body)
                self.evictions += 1
            self.items[key] = (body, headers, time.time() + ttl)
            self.size += len(body)

    def purge(self, key=None):
        with self.lock:
            if key is None:
                n = len(self.items)
                self.items.clear()
                self.size = 0
                return n
            if key in self.items:
                self._remove(key)
                return 1
            return 0

    def _remove(self, key):
        body, _, _ = self.items.pop(key)
        self.size -= len(body)

    def stats(self):
        with self.lock:
            total = self.hits + self.misses
            return {
                "objects": len(self.items),
                "used_bytes": self.size,
                "capacity_bytes": self.capacity,
                "hits": self.hits,
                "misses": self.misses,
                "hit_ratio": round(self.hits / total, 3) if total else 0.0,
                "evictions": self.evictions,
                "expired": self.expired,
                "keys": list(self.items.keys()),
            }


def parse_max_age(cache_control):
    for part in (cache_control or "").split(","):
        part = part.strip()
        if part in ("no-store", "private"):
            return 0
        if part.startswith("max-age="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                pass
    return 30


def run_edge(name):
    edge = find_edge(name)
    origin = CFG["origin"]
    cache = LRUCache(edge.get("cache_kb", 64) * 1024)

    class EdgeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/_health":
                return send_json(self, {"edge": name, "status": "ok"})
            if path == "/_stats":
                return send_json(self, {"edge": name, "ip": edge["ip"], **cache.stats()})

            cached = cache.get(path)
            if cached:
                body, headers, expires_at = cached
                return self._send(body, headers, "HIT", int(expires_at - time.time()))

            try:
                body, headers = self._fetch_from_origin(path)
            except urllib.error.HTTPError as e:
                self.send_error(e.code, f"Origin returned {e.code}")
                return
            except OSError as e:
                self.send_error(502, f"Origin unreachable: {e}")
                return

            ttl = parse_max_age(headers.get("Cache-Control"))
            if ttl > 0:
                cache.put(path, body, headers, ttl)
            self._send(body, headers, "MISS", ttl)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/_purge":
                self.send_error(404)
                return
            target = urllib.parse.parse_qs(parsed.query).get("path", [None])[0]
            n = cache.purge(target)
            log(name, f"purged {n} object(s) ({target or 'ALL'})")
            send_json(self, {"edge": name, "purged": n})

        def _fetch_from_origin(self, path):
            # Connect *from* this edge's own IP so the origin sees who is asking.
            conn = http.client.HTTPConnection(origin["ip"], origin["port"], timeout=5,
                                              source_address=(edge["ip"], 0))
            try:
                conn.request("GET", path, headers={
                    "Host": CFG["domain"], "X-CDN-Edge": name,
                    "X-Forwarded-For": self.client_address[0]})
                resp = conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    raise urllib.error.HTTPError(path, resp.status, resp.reason, None, None)
                headers = {k: v for k, v in resp.getheaders()
                           if k in ("Content-Type", "Cache-Control")}
                return body, headers
            finally:
                conn.close()

        def _send(self, body, headers, status, ttl):
            self.send_response(200)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Cache", status)
            self.send_header("X-Edge", f"{name} ({edge['ip']})")
            self.send_header("X-Cache-TTL-Remaining", str(max(ttl, 0)))
            self.end_headers()
            self.wfile.write(body)
            log(name, f"{self.client_address[0]:<15} {self.path:<20} {status}")

        def log_message(self, fmt, *args):
            if not args or str(args[1]) != "200":
                log(name, f"{self.client_address[0]:<15} {fmt % args}")

    serve_http(edge["ip"], edge["port"], EdgeHandler, name)


# --------------------------------------------------------------------------- #
# GeoDNS server
# --------------------------------------------------------------------------- #

def dns_parse_query(data):
    """Return (txid, flags, qname, qtype, question_bytes) from a DNS query."""
    txid, flags, qdcount = struct.unpack("!HHH", data[:6])
    if qdcount < 1:
        raise ValueError("no question")
    i, labels = 12, []
    while data[i] != 0:
        n = data[i]
        labels.append(data[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += 1 + n
    i += 1
    qtype, _qclass = struct.unpack("!HH", data[i:i + 4])
    return txid, flags, ".".join(labels).lower(), qtype, data[12:i + 4]


def dns_build_response(txid, flags, question, rcode=0, ip=None, ttl=0):
    rd = flags & 0x0100
    resp_flags = 0x8000 | 0x0400 | rd | rcode   # QR, AA, copy RD, RCODE
    ancount = 1 if ip else 0
    msg = struct.pack("!HHHHHH", txid, resp_flags, 1, ancount, 0, 0) + question
    if ip:
        msg += struct.pack("!HHHIH", 0xC00C, 1, 1, ttl, 4) + socket.inet_aton(ip)
    return msg


def dns_encode_query(name, txid):
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0) + q + struct.pack("!HH", 1, 1)


class GeoIP:
    def __init__(self, entries, default):
        self.nets = [(ipaddress.ip_network(e["cidr"]), e) for e in entries]
        self.default = default

    def locate(self, ip):
        addr = ipaddress.ip_address(ip)
        for net, entry in self.nets:
            if addr in net:
                return entry
        return self.default


def run_dns():
    cfg = CFG["dns"]
    domain = CFG["domain"].lower()
    geo = GeoIP(CFG["geoip"], CFG["default_location"])
    healthy = {e["name"]: True for e in CFG["edges"]}
    lock = threading.Lock()

    def health_loop():
        while True:
            for e in CFG["edges"]:
                try:
                    url = f"http://{e['ip']}:{e['port']}/_health"
                    with urllib.request.urlopen(url, timeout=1) as r:
                        ok = r.status == 200
                except OSError:
                    ok = False
                with lock:
                    if healthy[e["name"]] != ok:
                        log("dns", f"health: {e['name']} ({e['ip']}) is now "
                                   f"{'UP' if ok else 'DOWN'}")
                    healthy[e["name"]] = ok
            time.sleep(cfg.get("health_interval", 2))

    def pick_edge(client_ip):
        loc = geo.locate(client_ip)
        with lock:
            candidates = [e for e in CFG["edges"] if healthy[e["name"]]]
        if not candidates:
            return loc, None, None
        best = min(candidates, key=lambda e: haversine_km(loc["lat"], loc["lon"],
                                                          e["lat"], e["lon"]))
        return loc, best, haversine_km(loc["lat"], loc["lon"], best["lat"], best["lon"])

    threading.Thread(target=health_loop, daemon=True).start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((cfg["ip"], cfg["port"]))
    log("dns", f"authoritative for '{domain}' on udp://{cfg['ip']}:{cfg['port']}"
               f"  (pid {os.getpid()})")

    while True:
        try:
            data, (client_ip, client_port) = sock.recvfrom(512)
        except KeyboardInterrupt:
            break
        try:
            txid, flags, qname, qtype, question = dns_parse_query(data)
        except (ValueError, IndexError, struct.error):
            continue  # malformed packet; drop it

        if qname != domain and not qname.endswith("." + domain):
            sock.sendto(dns_build_response(txid, flags, question, rcode=3), (client_ip, client_port))
            log("dns", f"{client_ip:<15} {qname} -> NXDOMAIN")
            continue
        if qtype != 1:  # only A records; AAAA etc. get an empty NOERROR
            sock.sendto(dns_build_response(txid, flags, question), (client_ip, client_port))
            continue

        loc, edge, dist = pick_edge(client_ip)
        if edge is None:
            sock.sendto(dns_build_response(txid, flags, question, rcode=2), (client_ip, client_port))
            log("dns", f"{client_ip:<15} {qname} -> SERVFAIL (no healthy edges)")
            continue
        sock.sendto(dns_build_response(txid, flags, question, ip=edge["ip"], ttl=cfg["ttl"]),
                    (client_ip, client_port))
        log("dns", f"{client_ip:<15} [{loc['city']}] {qname} -> {edge['ip']} "
                   f"({edge['name']}, {dist:.0f} km)")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

def resolve(src_ip, name=None):
    """Send a real DNS query to the GeoDNS server from src_ip."""
    name = name or CFG["domain"]
    dns = CFG["dns"]
    txid = random.randint(0, 0xFFFF)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((src_ip, 0))
        s.settimeout(2)
        s.sendto(dns_encode_query(name, txid), (dns["ip"], dns["port"]))
        data, _ = s.recvfrom(512)
    rid, flags, _, ancount = struct.unpack("!HHHH", data[:8])
    if rid != txid or flags & 0x000F or ancount == 0:
        raise OSError(f"DNS lookup failed (rcode={flags & 0xF})")
    return socket.inet_ntoa(data[-4:])


def client_get(src_ip, path):
    """Resolve the CDN domain, then fetch path from the edge, all from src_ip."""
    t0 = time.perf_counter()
    edge_ip = resolve(src_ip)
    t1 = time.perf_counter()
    port = next(e["port"] for e in CFG["edges"] if e["ip"] == edge_ip)
    conn = http.client.HTTPConnection(edge_ip, port, timeout=10,
                                      source_address=(src_ip, 0))
    try:
        conn.request("GET", path, headers={"Host": CFG["domain"]})
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    t2 = time.perf_counter()
    return {"src": src_ip, "edge_ip": edge_ip, "status": resp.status,
            "edge": resp.getheader("X-Edge", "-"), "cache": resp.getheader("X-Cache", "-"),
            "bytes": len(body), "dns_ms": (t1 - t0) * 1000, "http_ms": (t2 - t1) * 1000}


def print_row(city, path, r):
    print(f"  {city:<10} {r['src']:<12} DNS->{r['edge_ip']:<12} {path:<19} "
          f"{r['cache']:<5} {r['status']}  {r['bytes']:>6} B  {r['http_ms']:6.1f} ms")


# --------------------------------------------------------------------------- #
# Orchestration: launch every role as its own process
# --------------------------------------------------------------------------- #

class Cluster:
    def __init__(self, log_to_files):
        self.procs = {}
        self.log_to_files = log_to_files

    def spawn(self, name, args):
        out = None
        if self.log_to_files:
            os.makedirs(LOG_DIR, exist_ok=True)
            out = open(os.path.join(LOG_DIR, f"{name}.log"), "w")
        self.procs[name] = subprocess.Popen(
            [sys.executable, "-u", os.path.abspath(__file__), *args],
            stdout=out, stderr=subprocess.STDOUT if out else None)

    def start(self):
        create_sample_content()
        self.spawn("origin", ["origin"])
        wait_tcp(CFG["origin"]["ip"], CFG["origin"]["port"])
        for e in CFG["edges"]:
            self.spawn(e["name"], ["edge", e["name"]])
        for e in CFG["edges"]:
            wait_tcp(e["ip"], e["port"])
        self.spawn("dns", ["dns"])
        time.sleep(0.5)

    def kill(self, name):
        p = self.procs.pop(name)
        p.terminate()
        p.wait()

    def stop(self):
        for name in list(self.procs):
            self.kill(name)


def wait_tcp(ip, port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((ip, port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{ip}:{port} did not come up")


def http_json(method, ip, port, path):
    req = urllib.request.Request(f"http://{ip}:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


CLIENTS = [(g["city"], g["cidr"].rsplit(".", 1)[0] + ".10") for g in CFG["geoip"]]


def run_demo():
    cluster = Cluster(log_to_files=True)
    cluster.start()
    print("Started processes:")
    for name, p in cluster.procs.items():
        ip = (CFG["origin"]["ip"] if name == "origin" else CFG["dns"]["ip"]
              if name == "dns" else find_edge(name)["ip"])
        print(f"  {name:<13} pid {p.pid:<7} {ip}")

    try:
        print("\n=== 1. GeoDNS routing by client IP + cache MISS then HIT ===")
        for city, ip in CLIENTS:
            for _ in range(2):
                print_row(city, "/index.html", client_get(ip, "/index.html"))

        paris = dict(CLIENTS)["Paris"]
        print("\n=== 2. Same edge, different client IPs share the cache ===")
        for host in (11, 22, 33):
            ip = paris.rsplit(".", 1)[0] + f".{host}"
            print_row("Paris", "/static/app.js", client_get(ip, "/static/app.js"))

        print("\n=== 3. LRU eviction (edge cache is "
              f"{find_edge('eu-west')['cache_kb']} KB) ===")
        for path in ["/images/logo.png", "/images/banner.jpg", "/images/hero.jpg",
                     "/images/logo.png"]:
            print_row("Paris", path, client_get(paris, path))
        eu = find_edge("eu-west")
        s = http_json("GET", eu["ip"], eu["port"], "/_stats")
        print(f"  eu-west holds {s['keys']} ({s['used_bytes']} B), "
              f"evictions={s['evictions']}")

        print("\n=== 4. Purge /images/logo.png on eu-west ===")
        print("  ", http_json("POST", eu["ip"], eu["port"], "/_purge?path=/images/logo.png"))
        print_row("Paris", "/images/logo.png", client_get(paris, "/images/logo.png"))

        print("\n=== 5. 404 passthrough ===")
        print_row("Paris", "/missing.txt", client_get(paris, "/missing.txt"))

        print(f"\n=== 6. Failover: kill eu-west process "
              f"(pid {cluster.procs['eu-west'].pid}) ===")
        cluster.kill("eu-west")
        time.sleep(CFG["dns"]["health_interval"] + 1.5)
        print_row("Paris", "/index.html", client_get(paris, "/index.html"))

        print("\n=== Summary ===")
        o = CFG["origin"]
        print(f"  Origin requests: {http_json('GET', o['ip'], o['port'], '/_stats')['origin_requests']}")
        print_stats()
        print(f"\n  Per-process logs: {LOG_DIR}/")
        print("  DNS decisions:")
        with open(os.path.join(LOG_DIR, "dns.log")) as f:
            for line in f:
                if "->" in line or "health" in line:
                    print("   ", line.rstrip())
    finally:
        cluster.stop()


def print_stats():
    for e in CFG["edges"]:
        try:
            s = http_json("GET", e["ip"], e["port"], "/_stats")
            print(f"  {e['name']:<13} {e['ip']:<12} hits={s['hits']:<3} misses={s['misses']:<3} "
                  f"hit_ratio={s['hit_ratio']:.2f} objects={s['objects']}")
        except OSError:
            print(f"  {e['name']:<13} {e['ip']:<12} DOWN")


def run_start():
    cluster = Cluster(log_to_files=False)
    def on_sigterm(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)
    cluster.start()
    print(f"\nCDN running. Try:\n"
          f"  dig @{CFG['dns']['ip']} -p {CFG['dns']['port']} {CFG['domain']} -b 127.1.2.10\n"
          f"  python3 cdn.py client 127.1.4.10 /index.html\n"
          f"  python3 cdn.py stats\n"
          "Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        cluster.stop()


def run_purge(target):
    q = "" if target == "all" else "?path=" + urllib.parse.quote(target)
    for e in CFG["edges"]:
        try:
            print(" ", http_json("POST", e["ip"], e["port"], "/_purge" + q))
        except OSError:
            print(f"  {e['name']}: DOWN")


def main():
    args = sys.argv[1:] or ["demo"]
    cmd = args[0]
    try:
        if cmd == "demo":
            run_demo()
        elif cmd == "start":
            run_start()
        elif cmd == "origin":
            run_origin()
        elif cmd == "edge" and len(args) == 2:
            run_edge(args[1])
        elif cmd == "dns":
            run_dns()
        elif cmd == "client" and len(args) == 3:
            r = client_get(args[1], args[2])
            print(json.dumps(r, indent=2))
        elif cmd == "stats":
            print_stats()
        elif cmd == "purge" and len(args) == 2:
            run_purge(args[1])
        else:
            print(__doc__)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
