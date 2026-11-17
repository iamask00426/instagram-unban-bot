"""Compare proxy pools with the bot's REAL request (GET + facebookexternalhit), and price them.

Run it once per provider/pool (the proxy line is typed hidden, never stored):

    python tools/pool_probe.py --banned banned.txt --active active.txt \
        --label proxiware-eco --price 0.30 --accounts 100 --every 120

    --price     $ per GB (0 for flat-rate/unlimited pools such as ISP or mobile ports)
    --accounts  how many accounts you plan to watch      --every  seconds between checks per account
    --per-minute  request pace of the test (default 60; keep it realistic for single static IPs)

banned.txt = names that do not exist / are banned,  active.txt = live accounts (one per line).

It prints how often a try was CONCLUSIVE (live profile with tags, or 404 / page without tags),
how often a LIVE account looked banned (false-ban risk - must be 0), the average size, and
the estimated monthly cost for your account count. Judge pools by "$ per 1,000 conclusive checks"
and by conclusive %, not by the provider's own success claims.
"""
import argparse, asyncio, collections, math, os, re, statistics, time
from urllib.parse import urlsplit

UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
LOGIN_TITLE = re.compile(rb"<title>[^<]*(log\s*in|login|challenge)", re.I)
GIB = 1024 ** 3


def load(path):
    with open(path) as f:
        return [l.strip().lstrip("@").lower() for l in f if l.strip()]


def classify(status, location, buf):
    """-> 'og' | '404' | 'noog' | 'blocked' | 'other'  (same meaning as the bot's checker)."""
    if status == 404:
        return "404"
    if status in (401, 403, 429) or (300 <= status < 400 and "login" in (location or "").lower()):
        return "blocked"
    if status == 200:
        if LOGIN_TITLE.search(buf):
            return "blocked"
        return "og" if b"og:title" in buf else "noog"
    return "other"


def summarize(rows, price, accounts, every, overhead=7000, safe_per_min=30):
    """rows: list of (kind_expected 'a'|'b', answer_kind, body_bytes, seconds). Returns a dict."""
    n = len(rows)
    ok = sum(1 for e, k, *_ in rows if (e == "a" and k == "og") or (e == "b" and k in ("404", "noog")))
    false_ban = sum(1 for e, k, *_ in rows if e == "a" and k in ("404", "noog"))
    blocked = sum(1 for _, k, *_ in rows if k == "blocked")
    rate = ok / n if n else 0.0
    avg_bytes = (statistics.mean(r[2] for r in rows) if rows else 0) + overhead
    out = {"n": n, "rate": rate, "false_ban": false_ban, "blocked": blocked / n if n else 0.0,
           "avg_kb": avg_bytes / 1024, "ms": (statistics.mean(r[3] for r in rows) * 1000) if rows else 0.0}
    tries_per_min = accounts * 60.0 / every / max(rate, 0.01)       # tries needed per minute for a conclusive check per account
    out["tries_per_min"] = tries_per_min
    if price > 0:
        out["usd_per_1000"] = avg_bytes / max(rate, 0.01) * 1000 * price / GIB
        out["usd_month"] = tries_per_min * 60 * 24 * 30 * avg_bytes * price / GIB
    else:
        out["ips_needed"] = math.ceil(tries_per_min / safe_per_min)
    return out


async def one(session, proxy, user, max_body=65536):
    import aiohttp
    url = f"https://www.instagram.com/{user}/"
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity", "Accept": "text/html"}
    t = time.monotonic()
    try:
        async with session.get(url, headers=headers, proxy=proxy, allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            n, buf = 0, b""
            if r.status == 200:
                while n < max_body:
                    chunk = await r.content.read(1024)
                    if not chunk:
                        break
                    n += len(chunk)
                    buf += chunk
                    if b"</head>" in buf or b"<body" in buf or LOGIN_TITLE.search(buf):
                        break
            return classify(r.status, r.headers.get("Location", ""), buf), n, time.monotonic() - t
    except Exception:
        return "blocked", 0, time.monotonic() - t


def ask_proxy():
    import getpass
    raw = (os.getenv("PROBE_PROXY") or getpass.getpass("Proxy line (host:port:user:pass), hidden: ")).strip()
    if raw.startswith("http"):
        return raw
    parts = raw.split(":", 3)
    if len(parts) != 4 or not parts[1].isdigit():
        raise SystemExit("Format must be host:port:user:pass")
    host, port, user, pw = parts
    return f"http://{user}:{pw}@{host}:{port}"


async def main():
    import aiohttp
    ap = argparse.ArgumentParser()
    ap.add_argument("--banned", required=True)
    ap.add_argument("--active", required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--label", default="pool")
    ap.add_argument("--price", type=float, default=1.0)
    ap.add_argument("--accounts", type=int, default=100)
    ap.add_argument("--every", type=float, default=120)
    ap.add_argument("--per-minute", type=float, default=60)
    ap.add_argument("--overhead", type=int, default=14000,
                    help="bytes billed per try on top of the page (handshake); ~14000 matches the Proxiware dashboard")
    a = ap.parse_args()
    proxy = ask_proxy()
    sp = urlsplit(proxy)
    print(f"Testing {a.label} via {sp.hostname}:{sp.port}")
    jobs = [(u, "b") for u in load(a.banned)] * a.rounds + [(u, "a") for u in load(a.active)] * a.rounds
    gap = 60.0 / a.per_minute
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as s:
        async def run(i, u, e):
            await asyncio.sleep(i * gap)
            k, n, sec = await one(s, proxy, u)
            return (e, k, n, sec)
        rows = await asyncio.gather(*(run(i, u, e) for i, (u, e) in enumerate(jobs)))
    r = summarize(rows, a.price, a.accounts, a.every, overhead=a.overhead)
    hist = collections.Counter(k for _, k, *_ in rows)
    print(f"\n{a.label}: {r['n']} tries")
    print(f"  conclusive: {100 * r['rate']:.0f}%   blocked/login wall: {100 * r['blocked']:.0f}%   avg {r['avg_kb']:.1f} KB/try   {r['ms']:.0f} ms")
    print(f"  answers: " + ", ".join(f"{k}={v}" for k, v in hist.most_common()))
    print(f"  LIVE accounts that looked banned: {r['false_ban']}   <- must be 0")
    if "usd_per_1000" in r:
        print(f"  ~${r['usd_per_1000']:.2f} per 1,000 conclusive checks")
        print(f"  ~${r['usd_month']:.0f}/month for {a.accounts} accounts every {a.every:.0f}s (one conclusive check each)")
    else:
        print(f"  flat-rate: ~{r['tries_per_min']:.0f} tries/min needed for {a.accounts} accounts every {a.every:.0f}s "
              f"-> about {r['ips_needed']} IPs/ports at ~30 req/min each (check the provider's monthly price per IP)")
    verdict = ("GOOD" if r["rate"] >= 0.7 and not r["false_ban"] else
               "UNSAFE (live accounts looked banned)" if r["false_ban"] else
               "OK for slow tier only" if r["rate"] >= 0.4 else "BAD")
    print(f"  verdict: {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
