"""
Benchmark: Current main (3 separate WS connections) vs optimized (1 connection).

Compares the two CDP coordinate click implementations against a real
Lightpanda WebSocket at ws://127.0.0.1:63372/.

  - Baseline (current main style): 3 separate _cdp_call() invocations, each
    opening a fresh WS connection (Target.getTargets, mousePressed, mouseReleased)
  - Optimized (this PR): single WS connection with all 4 messages pipelined
    (getTargets + attachToTarget + mousePressed+mouseReleased in one burst)

Also measures the agent-browser HTTP IPC round-trip as a reference point
for how fast the existing ref-based click path is.

Usage:
    python scripts/benchmark_click_paths.py
    python scripts/benchmark_click_paths.py --iterations 300 --warmup 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from statistics import mean, median, stdev
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, "/private/tmp/hermes-coord-click")

LIGHTPANDA_WS = "ws://127.0.0.1:63372/"
AGENT_BROWSER_PORT = 63371


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stats(times_s: List[float]) -> Dict:
    ms = [t * 1000 for t in times_s]
    return {
        "mean_ms":   mean(ms),
        "median_ms": median(ms),
        "min_ms":    min(ms),
        "max_ms":    max(ms),
        "stdev_ms":  stdev(ms) if len(ms) > 1 else 0.0,
        "p95_ms":    sorted(ms)[int(len(ms) * 0.95)],
    }


def _bench(fn, warmup: int, n: int) -> Tuple[List[float], int]:
    for _ in range(warmup):
        fn()
    times, errors = [], 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            result = fn()
            elapsed = time.perf_counter() - t0
            if isinstance(result, str):
                d = json.loads(result)
                if not d.get("success"):
                    errors += 1
        except Exception:
            elapsed = time.perf_counter() - t0
            errors += 1
        times.append(elapsed)
    return times, errors


def _row(label: str, stats: Dict, col_w: int = 9) -> None:
    print(
        f"  {label:<46}  "
        f"{stats['mean_ms']:>{col_w}.2f}  "
        f"{stats['median_ms']:>{col_w}.2f}  "
        f"{stats['min_ms']:>{col_w}.2f}  "
        f"{stats['p95_ms']:>{col_w}.2f}  "
        f"{stats['max_ms']:>{col_w}.2f}  ms"
    )


# ---------------------------------------------------------------------------
# The "current main" approach — 3 separate _cdp_call() connections
# ---------------------------------------------------------------------------

def _baseline_cdp_click(endpoint: str, x: int, y: int, button: str = "left") -> str:
    """Replicate the previous 3-connection approach from the original PR."""
    from tools.browser_cdp_tool import _cdp_call, _run_async

    try:
        targets_result = _run_async(_cdp_call(endpoint, "Target.getTargets", {}, None, 10.0))
        page_target = None
        for t in targets_result.get("targetInfos", []):
            if t.get("type") == "page" and t.get("attached", True):
                page_target = t["targetId"]
                break
    except Exception:
        page_target = None

    mouse_params = {"type": "", "x": x, "y": y, "button": button, "clickCount": 1}
    try:
        _run_async(_cdp_call(endpoint, "Input.dispatchMouseEvent",
                             {**mouse_params, "type": "mousePressed"}, page_target, 10.0))
        _run_async(_cdp_call(endpoint, "Input.dispatchMouseEvent",
                             {**mouse_params, "type": "mouseReleased"}, page_target, 10.0))
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
    return json.dumps({"success": True, "clicked_at": {"x": x, "y": y}, "method": "baseline"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_benchmark(iterations: int = 300, warmup: int = 20) -> None:
    print(f"\n{'=' * 78}")
    print(f"  browser_click Coordinate Click: Current Main vs Optimized (1-conn)")
    print(f"  Real Lightpanda WS: {LIGHTPANDA_WS}")
    print(f"{'=' * 78}")
    print(f"  Iterations: {iterations}  |  Warmup: {warmup}")

    # pre-flight
    try:
        with urllib.request.urlopen("http://127.0.0.1:63372/json/version", timeout=2) as r:
            info = json.loads(r.read())
            assert "webSocketDebuggerUrl" in info
        print(f"  ✓ Lightpanda CDP: {info.get('webSocketDebuggerUrl')}")
    except Exception as e:
        print(f"  ✗ Lightpanda not reachable: {e}")
        return

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{AGENT_BROWSER_PORT}/api/sessions", timeout=2) as r:
            sessions = json.loads(r.read())
        print(f"  ✓ agent-browser: {len(sessions)} session(s)")
        ab_ok = True
    except Exception:
        print(f"  ⚠  agent-browser not reachable — ref-click IPC baseline skipped")
        ab_ok = False

    import importlib
    import tools.browser_tool as bt
    import tools.browser_cdp_tool as cdp_mod
    importlib.reload(cdp_mod)
    importlib.reload(bt)
    bt._is_camofox_mode = lambda: False
    _orig_resolve = cdp_mod._resolve_cdp_endpoint

    # -----------------------------------------------------------------------
    # 1. Baseline: current-main 3-connection approach
    # -----------------------------------------------------------------------
    print(f"\n  [1/3] Baseline (current main — 3 separate WS connections per click)")
    print(f"        Warmup {warmup}, then {iterations} iterations...")

    base_times, base_err = _bench(
        lambda: _baseline_cdp_click(LIGHTPANDA_WS, 150, 200),
        warmup, iterations,
    )
    base_stats = _stats(base_times)
    print(f"        Done — {base_err} errors, mean={base_stats['mean_ms']:.2f}ms")

    # -----------------------------------------------------------------------
    # 2. Optimized: single-connection — first-click cost (cold cache)
    # -----------------------------------------------------------------------
    print(f"\n  [2/3] Optimized — cold cache (1 WS conn, includes getTargets+attachToTarget)")
    print(f"        {iterations} iterations, cache cleared before each...")

    def _cold_click():
        bt._CDP_SESSION_CACHE.clear()
        return bt.browser_click(x=150.0, y=200.0, task_id="bench")

    cdp_mod._resolve_cdp_endpoint = lambda: LIGHTPANDA_WS
    cold_times, cold_err = _bench(_cold_click, warmup=0, n=iterations)
    cold_stats = _stats(cold_times)
    print(f"        Done — {cold_err} errors, mean={cold_stats['mean_ms']:.2f}ms")

    # -----------------------------------------------------------------------
    # 3. Optimized: warm cache (session cached from previous click)
    # -----------------------------------------------------------------------
    print(f"\n  [3/3] Optimized — warm cache (1 WS conn, skips getTargets+attachToTarget)")
    print(f"        Warmup {warmup} (fills cache), then {iterations} iterations...")

    bt._CDP_SESSION_CACHE.clear()
    opt_times, opt_err = _bench(
        lambda: bt.browser_click(x=150.0, y=200.0, task_id="bench"),
        warmup, iterations,
    )
    cdp_mod._resolve_cdp_endpoint = _orig_resolve
    opt_stats = _stats(opt_times)
    print(f"        Done — {opt_err} errors, mean={opt_stats['mean_ms']:.2f}ms")

    # -----------------------------------------------------------------------
    # 4. agent-browser HTTP IPC reference (what a ref click costs)
    # -----------------------------------------------------------------------
    if ab_ok:
        print(f"\n  [ref] agent-browser HTTP IPC (reference for ref-click latency)")
        ab_times = []
        for _ in range(warmup):
            urllib.request.urlopen(f"http://127.0.0.1:{AGENT_BROWSER_PORT}/api/sessions", timeout=5).read()
        for _ in range(iterations):
            t0 = time.perf_counter()
            urllib.request.urlopen(f"http://127.0.0.1:{AGENT_BROWSER_PORT}/api/sessions", timeout=5).read()
            ab_times.append(time.perf_counter() - t0)
        ab_stats = _stats(ab_times)
        print(f"        Done — mean={ab_stats['mean_ms']:.2f}ms")

    # -----------------------------------------------------------------------
    # Results
    # -----------------------------------------------------------------------
    col_w = 9
    print(f"\n{'─' * 78}")
    print(f"  {'Approach':<46}  {'Mean':>{col_w}}  {'Median':>{col_w}}  {'Min':>{col_w}}  {'p95':>{col_w}}  {'Max':>{col_w}}")
    print(f"{'─' * 78}")
    _row("Baseline  (3 WS connections, sequential)     ", base_stats, col_w)
    _row("Optimized — cold cache (1 conn + negotiate)  ", cold_stats, col_w)
    _row("Optimized — warm cache (1 conn, skip resolve)", opt_stats,  col_w)
    if ab_ok:
        _row("Ref-click IPC baseline (1 HTTP req)          ", ab_stats,  col_w)
    print(f"{'─' * 78}")

    print(f"\n  Speedups (mean vs baseline):")
    print(f"    Cold cache:  {base_stats['mean_ms'] / cold_stats['mean_ms']:.2f}x  ({base_stats['mean_ms'] - cold_stats['mean_ms']:.2f} ms saved)")
    print(f"    Warm cache:  {base_stats['mean_ms'] / opt_stats['mean_ms']:.2f}x  ({base_stats['mean_ms'] - opt_stats['mean_ms']:.2f} ms saved)")
    saved_by_cache = cold_stats['mean_ms'] - opt_stats['mean_ms']
    print(f"    Cache saves: {saved_by_cache:.2f} ms/click (Target.getTargets + Target.attachToTarget skipped)")

    if ab_ok:
        cdp_vs_ref = opt_stats["mean_ms"] / ab_stats["mean_ms"]
        print(f"\n    Warm-cached CDP vs ref-click: {cdp_vs_ref:.1f}x  (+{opt_stats['mean_ms'] - ab_stats['mean_ms']:.2f} ms)")
        print(f"    Remaining gap = cost of 1 WS connection open.")

    print(f"\n  Summary of optimizations in this PR:")
    print(f"    1. Single WS connection   — eliminates 2 TCP+WS handshakes per click")
    print(f"    2. mouseReleased-only wait — skips 1 RTT (press ack redundant per Playwright)")
    print(f"    3. Session ID cache       — eliminates getTargets+attachToTarget on repeat clicks")
    print(f"    4. compression=None       — no compression overhead on small CDP messages")
    print(f"    (Browser-harness, Playwright, and Puppeteer all use variations of these same patterns)")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    args = parser.parse_args()
    run_benchmark(iterations=args.iterations, warmup=args.warmup)
