"""
Shared Polygon API rate limiter for all KIBO agents.
Free tier: 5 calls/minute = one call every 12 seconds minimum.
All agents import this and call wait_for_slot() before any Polygon request.
"""
import time
import threading

# Polygon free tier = 5 calls per 60 seconds
MAX_CALLS    = 5
WINDOW_SECS  = 60
MIN_GAP_SECS = 13  # 13s between calls to be safe

_lock        = threading.Lock()
_call_times  = []

def wait_for_slot(caller="unknown"):
    """Block until it's safe to make a Polygon API call."""
    with _lock:
        now = time.time()
        # Remove calls older than the window
        global _call_times
        _call_times = [t for t in _call_times if now - t < WINDOW_SECS]

        # If we've hit the max, wait until the oldest call expires
        if len(_call_times) >= MAX_CALLS:
            wait = WINDOW_SECS - (now - _call_times[0]) + 1
            if wait > 0:
                print(f"[rate_limiter] {caller}: waiting {wait:.1f}s (rate limit)")
                time.sleep(wait)
                now = time.time()
                _call_times = [t for t in _call_times if now - t < WINDOW_SECS]

        # Enforce minimum gap between any two calls
        if _call_times:
            gap = now - _call_times[-1]
            if gap < MIN_GAP_SECS:
                time.sleep(MIN_GAP_SECS - gap)

        _call_times.append(time.time())
