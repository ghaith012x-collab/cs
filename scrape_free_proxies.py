#!/usr/bin/env python3
"""Scrape free proxies from public lists, validate them, save the working ones.

No external deps (stdlib only: urllib + concurrent.futures).

Outputs:
  free_proxies_raw.txt      -> every unique candidate found (ip:port per line)
  free_proxies.txt          -> validated working proxies (ip:port per line)
  free_proxies_checked.txt  -> working proxies with scheme + latency (ms)

Usage: python3 scrape_free_proxies.py
"""
import concurrent.futures as cf
import re
import sys
import time
import urllib.request

SOURCES = [
    # HTML tables
    "https://free-proxy-list.net/",
    "https://www.sslproxies.org/",
    "https://www.us-proxy.org/",
    "https://www.socks-proxy.net/",
    # Plain-text APIs
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all",
    # GitHub raw lists (frequently updated)
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
]

IP_PORT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}\b")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def is_public_ip(ip):
    """Reject private/reserved ranges so we don't store garbage."""
    try:
        a, b, c, d = (int(p) for p in ip.split("."))
    except ValueError:
        return False
    if a == 0 or a == 10 or a == 127:                       # 0.x, 10.x, loopback
        return False
    if a == 169 and b == 254:                               # link-local
        return False
    if a == 172 and 16 <= b <= 31:                          # 172.16-31.x
        return False
    if a == 192 and b == 168:                               # 192.168.x
        return False
    if a >= 224:                                            # multicast/reserved
        return False
    return True


def fetch(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [skip] {url} -> {type(e).__name__}")
    return None


def scrape():
    found = set()
    for url in SOURCES:
        text = fetch(url)
        if not text:
            continue
        for hit in IP_PORT_RE.findall(text):
            ip, port = hit.rsplit(":", 1)
            if is_public_ip(ip):
                found.add(hit)
        print(f"  [ok]   {url} (cumulative {len(found)})")
    return sorted(found)


def check(proxy, timeout=6):
    """Try the proxy against two targets; return (proxy, latency_ms) or (proxy, None)."""
    targets = ("http://httpbin.org/ip", "https://api.ipify.org")
    for target in targets:
        try:
            t0 = time.time()
            handler = urllib.request.ProxyHandler({
                "http": f"http://{proxy}",
                "https": f"http://{proxy}",
            })
            opener = urllib.request.build_opener(handler)
            req = urllib.request.Request(target, headers=UA)
            with opener.open(req, timeout=timeout) as resp:
                resp.read(256)
            return (proxy, int((time.time() - t0) * 1000))
        except Exception:
            continue
    return (proxy, None)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500,
                    help="max candidates to validate (keep low in sandboxes)")
    ap.add_argument("--workers", type=int, default=80)
    args = ap.parse_args()

    print("Scraping free proxy lists...")
    candidates = scrape()
    print(f"\nFound {len(candidates)} unique candidates")
    if len(candidates) > args.limit:
        candidates = candidates[:args.limit]
        print(f"Limiting validation to {args.limit} candidates")

    if not candidates:
        print("No candidates — outbound network to proxy lists appears blocked in this sandbox.")
        print("Run this script on the cosyra box / a VPS:  python3 scrape_free_proxies.py")
        sys.exit(1)

    with open("free_proxies_raw.txt", "w") as f:
        f.write("\n".join(candidates) + "\n")
    print(f"Saved raw candidates -> free_proxies_raw.txt")

    print(f"Validating concurrently ({args.workers} workers, 6s timeout each)...")
    working = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for proxy, ms in ex.map(check, candidates):
            if ms is not None:
                working.append((proxy, ms))
    working.sort(key=lambda t: t[1])

    print(f"\nWORKING: {len(working)}/{len(candidates)}")
    with open("free_proxies.txt", "w") as f:
        for proxy, _ in working:
            f.write(f"{proxy}\n")
    with open("free_proxies_checked.txt", "w") as f:
        for proxy, ms in working:
            f.write(f"{proxy}  {ms}ms\n")

    print("Saved -> free_proxies.txt (working) and free_proxies_checked.txt (with latency)")
    print("\nFastest 15:")
    for proxy, ms in working[:15]:
        print(f"  {proxy}  {ms}ms")


if __name__ == "__main__":
    main()
