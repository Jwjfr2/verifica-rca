"""Batch lookups: one warm session per worker, proxies rotated on WAF blocks.

    python batch.py GA123BC GX999ZZ                  # sequential, one session
    cat plates.txt | python batch.py
    python batch.py --proxy-file proxies.txt < plates.txt
    python batch.py --proxy-file proxies.txt --workers 4 < plates.txt

Per-lookup latency is dominated by the portal (TLS + two round trips), so throughput
scales with parallel sessions — one session per proxy, since a captcha challenge is bound
to its JSESSIONID. `--workers > 1` therefore requires at least that many proxies unless
`--allow-shared-ip` is given (parallel traffic from one IP is what trips the WAF).
"""
import argparse
import queue
import sys
import threading
import time

from rca import Blocked, Portal, format_line, query_all


def format_result(plate, res, dt):
    return f"{plate}: {format_line(res)}"


def worker(wid, plates, proxies, tipo, out, lock, proxy_state):
    portal = None
    while True:
        try:
            plate = plates.get_nowait()
        except queue.Empty:
            return
        for _ in range(len(proxies)):
            pi = proxy_state[wid]
            try:
                if portal is None:
                    portal = Portal(proxy=proxies[pi])
                t0 = time.time()
                res = query_all(plate, tipo=tipo, portal=portal)
            except Blocked:
                line = f"{plate}: WAF block on {proxies[pi] or 'direct'}; rotating"
                portal = None
                proxy_state[wid] = (pi + 1) % len(proxies)
                with lock:
                    out.append(line)
                continue
            except Exception as e:
                portal = None
                proxy_state[wid] = (pi + 1) % len(proxies)
                with lock:
                    out.append(f"{plate}: {type(e).__name__}: {str(e)[:60]}; rotating")
                continue
            with lock:
                out.append(format_result(plate, res, time.time() - t0))
            break
        else:
            with lock:
                out.append(f"{plate}: all proxies blocked")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("plates", nargs="*")
    ap.add_argument("--tipo", default="A")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--allow-shared-ip", action="store_true",
                    help="allow --workers > 1 with fewer proxies than workers")
    args = ap.parse_args(argv)

    plates = args.plates or [l.strip() for l in sys.stdin if l.strip()]
    proxies = [args.proxy] if args.proxy else []
    if args.proxy_file:
        proxies += [l.strip() for l in open(args.proxy_file) if l.strip() and not l.startswith("#")]
    proxies = proxies or [None]

    workers = max(1, min(args.workers, len(plates) or 1))
    if workers > 1 and len(proxies) < workers and not args.allow_shared_ip:
        ap.error(f"--workers {workers} needs {workers} proxies (got {len(proxies)}); "
                 f"use --allow-shared-ip to override")

    work = queue.Queue()
    for p in plates:
        work.put(p)
    out, lock = [], threading.Lock()
    proxy_state = {i: i % len(proxies) for i in range(workers)}

    t0 = time.time()
    threads = [threading.Thread(target=worker,
                                args=(i, work, proxies, args.tipo, out, lock, proxy_state),
                                daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for line in out:
        print(line, flush=True)
    if plates:
        dt = time.time() - t0
        print(f"-- {len(plates)} plate(s) in {dt:.1f}s ({dt/len(plates):.1f} s/plate, "
              f"{len(proxies)} proxy/proxies, {workers} worker(s))", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
