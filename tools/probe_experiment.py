"""Step 2 experiment: which request type is the cheapest reliable ban/unban check?

Usage (proxy comes from the environment, never hardcode it):
    PROBE_PROXY="http://user:pass@host:port" python tools/probe_experiment.py \
        --banned banned.txt --active active.txt [--rounds 2] [--max-body 65536]

banned.txt = usernames you KNOW are banned/unavailable (one per line)
active.txt = usernames you KNOW are live

For each variant (HEAD/GET x user-agent) it prints: how often the status code alone gave the right
answer, how often the answer was a login wall/blocked, average body bytes read, average latency.
If HEAD (or a small UA) classifies correctly ~all the time, a check can cost ~1-2 KB instead of ~60 KB.
"""
import argparse, asyncio, os, statistics, time
import aiohttp

UAS = {
    "fb": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "twitter": "Twitterbot/1.0",
    "whatsapp": "WhatsApp/2.23.20.0 A",
}
VARIANTS = [(m, ua) for m in ("HEAD", "GET") for ua in UAS]


def load(path):
    with open(path) as f:
        return [l.strip().lstrip("@").lower() for l in f if l.strip()]


async def one(session, proxy, method, ua, user, max_body):
    url = f"https://www.instagram.com/{user}/"
    headers = {"User-Agent": UAS[ua], "Accept-Language": "en-US,en;q=0.9",
               "Accept-Encoding": "gzip, deflate", "Accept": "text/html"}
    t = time.monotonic()
    try:
        async with session.request(method, url, headers=headers, proxy=proxy, allow_redirects=False,
                                   auto_decompress=False, timeout=aiohttp.ClientTimeout(total=8)) as r:
            n = 0
            if method == "GET" and r.status == 200:
                while n < max_body:
                    chunk = await r.content.read(1024)
                    if not chunk:
                        break
                    n += len(chunk)
                    if b"</head>" in chunk or b"<body" in chunk:
                        break
            loc = r.headers.get("Location", "")
            blocked = r.status in (401, 403, 429) or (300 <= r.status < 400 and "login" in loc.lower())
            return r.status, blocked, n, time.monotonic() - t
    except Exception as e:
        return type(e).__name__, True, 0, time.monotonic() - t


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--banned", required=True)
    ap.add_argument("--active", required=True)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--max-body", type=int, default=65536)
    a = ap.parse_args()
    proxy = os.getenv("PROBE_PROXY") or None
    banned, active = load(a.banned), load(a.active)
    sem = asyncio.Semaphore(8)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as s:
        print(f"{'variant':<18}{'banned->404':>12}{'active->200':>12}{'blocked':>9}{'avg body KB':>13}{'avg ms':>8}")
        for method, ua in VARIANTS:
            async def run(u):
                async with sem:
                    return await one(s, proxy, method, ua, u, a.max_body)
            jobs = [(u, "b") for u in banned] * a.rounds + [(u, "a") for u in active] * a.rounds
            res = await asyncio.gather(*(run(u) for u, _ in jobs))
            pairs = list(zip([k for _, k in jobs], res))
            def pct(kind, code):
                xs = [r for k, r in pairs if k == kind]
                return 100 * sum(1 for r in xs if r[0] == code) // max(1, len(xs))
            blocked = 100 * sum(1 for _, r in pairs if r[1]) // max(1, len(pairs))
            kb = statistics.mean(r[2] for _, r in pairs) / 1024
            ms = statistics.mean(r[3] for _, r in pairs) * 1000
            print(f"{method + '/' + ua:<18}{pct('b', 404):>11}%{pct('a', 200):>11}%{blocked:>8}%{kb:>13.1f}{ms:>8.0f}")


if __name__ == "__main__":
    asyncio.run(main())
