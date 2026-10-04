#!/usr/bin/env python3
"""Measure cold download, warmup-hit startup, parallel clients and queue saturation.

    uv run --no-project scripts/benchmark_jukes.py http://127.0.0.1:5000 [--videos ID ...] [--parallel 8]

Run against the VM first, then the Cloudflare hostname. Uses only the public
API. Nothing here proves Android playback or Google capture; it reports timings.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_VIDEOS = ["lYBUbBu4W08", "9bZkp7q19f0", "kJQP7kiw5Fk", "JGwWNGJdvx8"]


def call(base, method, path, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, payload, time.perf_counter() - started, dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), time.perf_counter() - started, dict(err.headers)


def prepare_until_ready(base, video_id, route="/v1/audio/prepare", deadline=150):
    status, body, took, _ = call(base, "POST", route, {"video_id": video_id})
    started = time.perf_counter() - took
    info = json.loads(body or b"{}")
    while status in (200, 202) and info.get("status") not in ("ready", "failed"):
        if time.perf_counter() - started > deadline:
            return "timeout", time.perf_counter() - started
        time.sleep(1)
        status, body, _, _ = call(base, "GET", f"/v1/jobs/{info['job_id']}")
        info = json.loads(body or b"{}")
    return info.get("status") or f"http_{status}", time.perf_counter() - started


def first_byte(base, video_id):
    status, body, took, headers = call(base, "GET", f"/v1/audio/{video_id}", headers={"Range": "bytes=0-1023"})
    return status, took


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("--videos", nargs="*", default=DEFAULT_VIDEOS)
    parser.add_argument("--parallel", type=int, default=8)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    results = {}

    vid = args.videos[0]
    state, took = prepare_until_ready(base, vid)
    results["cold_download"] = {"video": vid, "state": state, "seconds": round(took, 2)}

    vid2 = args.videos[1]
    state, warm_took = prepare_until_ready(base, vid2, "/v1/warmup")
    status, play = first_byte(base, vid2)  # warmup hit: should be one fast range request
    results["warmup_hit_startup"] = {"video": vid2, "warmup_state": state, "warmup_seconds": round(warm_took, 2),
                                     "first_range_status": status, "first_range_seconds": round(play, 3)}

    cached = [first_byte(base, vid)[1] for _ in range(5)]
    results["cached_first_range_seconds"] = {"median": round(statistics.median(cached), 3), "max": round(max(cached), 3)}

    def worker(_):
        return first_byte(base, vid)

    started = time.perf_counter()
    with ThreadPoolExecutor(args.parallel) as pool:
        outcomes = list(pool.map(worker, range(args.parallel * 3)))
    results["parallel_clients"] = {"clients": args.parallel, "requests": len(outcomes),
                                   "ok": sum(1 for s, _ in outcomes if s in (200, 206)),
                                   "wall_seconds": round(time.perf_counter() - started, 2),
                                   "p95_seconds": round(sorted(t for _, t in outcomes)[int(len(outcomes) * 0.95) - 1], 3)}

    extra = args.videos[2:]
    statuses = []
    with ThreadPoolExecutor(max(1, len(extra))) as pool:
        for status, body, _, headers in pool.map(
                lambda v: call(base, "POST", "/v1/audio/prepare", {"video_id": v}), extra):
            statuses.append(status)
    results["job_saturation_probe"] = {"submitted": len(extra), "statuses": statuses,
                                       "note": "202 = accepted; 429 = admission limit; 503 = capacity"}
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
