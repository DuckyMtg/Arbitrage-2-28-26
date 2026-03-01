# app/services/ev_cache.py
from __future__ import annotations

import json
import os
import time
import hashlib
from typing import Any, Optional, Callable

import redis


# Bump this when EV logic/probabilities/queries change
MODEL_VERSION = os.getenv("EV_MODEL_VERSION", "2026-02-08.1")

# TTLs (seconds)
TTL_EV = 65 * 60           # 65 minutes
TTL_AVG = 65 * 60          # 65 minutes
TTL_CARDS = 7 * 24 * 3600  # 7 days

LOCK_TTL = 300              # seconds (how long we hold the compute lock)
LOCK_WAIT = 2.0            # seconds (how long other callers wait)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_client: Optional[redis.Redis] = None


def redis_client() -> redis.Redis:
    """
    Lazily constructed Redis client.
    Important: Redis.from_url() does not necessarily connect immediately;
    errors typically happen when commands run. We still keep this simple.
    """
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,  # store/read as str
            socket_connect_timeout=2,
            socket_timeout=5,
        )
    return _client


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def key_ev(set_code: str) -> str:
    return f"ev:{MODEL_VERSION}:{set_code.upper()}"


def key_cards(query: str, unique: str) -> str:
    return f"cards:{MODEL_VERSION}:{unique}:{_sha1(query)}"


def key_avg(query: str, unique: str, price_field: str) -> str:
    return f"avg:{MODEL_VERSION}:{unique}:{price_field}:{_sha1(query)}"


def key_lock(name: str) -> str:
    return f"lock:{name}"


def cache_get_json(k: str) -> Optional[Any]:
    """
    Fail-open cache read: if Redis is down/unreachable, treat as cache miss.
    """
    try:
        r = redis_client()
        val = r.get(k)
    except Exception:
        return None

    if val is None:
        return None
    try:
        return json.loads(val)
    except json.JSONDecodeError:
        return None


def cache_set_json(k: str, obj: Any, ttl: int) -> None:
    """
    Fail-open cache write: if Redis is down/unreachable, skip caching.
    """
    try:
        r = redis_client()
        r.setex(k, ttl, json.dumps(
            obj, separators=(",", ":"), ensure_ascii=False))
    except Exception:
        return


class RedisLock:
    """
    Simple Redis lock using SET NX EX.
    Fail-open: if Redis is down, acquire() returns False (no lock),
    and release() never raises.
    Not re-entrant; good enough to prevent stampedes when Redis is available.
    """

    def __init__(self, lock_key: str, ttl_s: int = LOCK_TTL):
        self.lock_key = lock_key
        self.ttl_s = ttl_s
        self.acquired = False

        # Hold a client reference if we can. If anything goes weird, fail open.
        try:
            self.r: Optional[redis.Redis] = redis_client()
        except Exception:
            self.r = None

    def acquire(self) -> bool:
        """
        Return True iff we successfully acquired the lock.
        If Redis is unavailable, return False (fail-open; caller will compute).
        """
        if self.r is None:
            self.acquired = False
            return False

        try:
            ok = self.r.set(self.lock_key, "1", nx=True, ex=self.ttl_s)
            self.acquired = bool(ok)
            return self.acquired
        except Exception:
            # Redis down / timeout / command error => behave as "no lock"
            self.acquired = False
            return False

    def release(self) -> None:
        """
        Never raises. If Redis is unavailable, just clear local state.
        """
        if not self.acquired:
            return

        try:
            if self.r is not None:
                self.r.delete(self.lock_key)
        except Exception:
            # Ignore Redis errors on release; fail-open behavior
            pass
        finally:
            self.acquired = False


def wait_for_key(k: str, wait_s: float = LOCK_WAIT) -> Optional[Any]:
    """
    Wait briefly for another request to populate the cache.
    This is also fail-open because cache_get_json is fail-open.
    """
    deadline = time.time() + wait_s
    while time.time() < deadline:
        v = cache_get_json(k)
        if v is not None:
            return v
        time.sleep(0.05)
    return None


# -------------------------------------------------------------------
# NEW: Shared cache-or-compute helpers (used by /v1/ev and deals endpoints)
# -------------------------------------------------------------------

def get_or_compute_json(
    *,
    cache_key: str,
    lock_name: str,
    ttl_s: int,
    compute_fn: Callable[[], Any],
    wait_s: float = LOCK_WAIT,
) -> Any:
    """
    Generic 'cache-or-compute' helper with best-effort stampede protection.

    - Fast path: return cached value if present.
    - Best-effort lock: if not acquired, wait briefly for another request
      to populate the cache; if still missing, compute anyway.
    - If lock acquired: compute, cache, return.

    Fully fail-open if Redis is down: compute_fn() still runs and returns a response.
    """
    # 1) Fast path: cache hit
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached

    # 2) Best-effort lock
    lock = RedisLock(key_lock(lock_name), ttl_s=LOCK_TTL)

    if not lock.acquire():
        # Someone else might be computing, or Redis is down. Wait briefly.
        waited = wait_for_key(cache_key, wait_s=wait_s)
        if waited is not None:
            return waited

        # Still missing => compute without lock
        data = compute_fn()
        cache_set_json(cache_key, data, ttl_s)
        return data

    try:
        # Double-check cache after acquiring lock (another worker might have filled it)
        cached2 = cache_get_json(cache_key)
        if cached2 is not None:
            return cached2

        data = compute_fn()
        cache_set_json(cache_key, data, ttl_s)
        return data
    finally:
        lock.release()


def get_or_compute_ev_report(set_code: str, compute_fn: Callable[[], dict]) -> dict:
    """
    EV-specific wrapper so every endpoint uses the same Redis key + lock logic.

    compute_fn must return a JSON-serializable dict (ex: dataclasses.asdict(report)).
    """
    code = set_code.strip().upper()
    return get_or_compute_json(
        cache_key=key_ev(code),
        lock_name=f"ev:{code}",
        ttl_s=TTL_EV,
        compute_fn=compute_fn,
        wait_s=LOCK_WAIT,
    )
