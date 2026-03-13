# app/services/rate_limit.py
from __future__ import annotations

import os
import time

from fastapi import Header, HTTPException, status

from app.services import ev_cache

# ---------------------------------------------------------------------------
# Configuration via environment variables
#   RATE_LIMIT_REQUESTS        max requests per window  (default: 60)
#   RATE_LIMIT_WINDOW_SECONDS  window size in seconds   (default: 60)
# ---------------------------------------------------------------------------
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "60"))
RATE_LIMIT_WINDOW_S = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))


def require_rate_limit(x_api_key: str | None = Header(default=None)) -> None:
    """
    Fixed-window rate limiter backed by Redis, used as a FastAPI dependency.

    - Keyed per API key, so different callers have independent buckets.
    - Fails open: if Redis is unavailable, requests are NOT blocked.
    - The TTL on the Redis key is set to 2× the window to handle the boundary
      race where a key is read just before it would have expired.

    Usage (on any router that already has require_api_key):
        dependencies=[Depends(require_api_key), Depends(require_rate_limit)]
    """
    if not x_api_key:
        # require_api_key handles the missing/invalid key case; nothing to do here.
        return

    window_bucket = int(time.time() // RATE_LIMIT_WINDOW_S)
    redis_key = f"ratelimit:{x_api_key}:{window_bucket}"

    try:
        r = ev_cache.redis_client()
        count = r.incr(redis_key)
        if count == 1:
            # First request in this window — set TTL so the key self-expires.
            r.expire(redis_key, RATE_LIMIT_WINDOW_S * 2)

        if count > RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded: {RATE_LIMIT_REQUESTS} requests per "
                    f"{RATE_LIMIT_WINDOW_S}s window. Please slow down."
                ),
            )
    except HTTPException:
        raise  # re-raise 429s — don't swallow them
    except Exception:
        pass  # fail open if Redis is down
