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
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=5,
        )
    return _client


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def key_ev(set_code: str, kind: str = "box") -> str:
    return f"ev:{MODEL_VERSION}:{set_code.upper()}:{kind.lower()}"


def key_cards(query: str, unique: str) -> str:
    return f"cards:{MODEL_VERSION}:{unique}:{_sha1(query)}"


def key_avg(query: str, unique: str, price_field: str) -> str:
    return f"avg:{MODEL_VERSION}:{unique}:{price_field}:{_sha1(query)}"


def key_lock(name: str) -> str:
    return f"lock:{name}"


def cache_get_json(k: str) -> Optional[Any]:
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
    try:
        r = redis_client()
        r.setex(k, ttl, json.dumps(
            obj, separators=(",", ":"), ensure_ascii=False))
    except Exception:
        return


class RedisLock:
    def __init__(self, lock_key: str, ttl_s: int = LOCK_TTL):
        self.lock_key = lock_key
        self.ttl_s = ttl_s
        self.acquired = False

        try:
            self.r: Optional[redis.Redis] = redis_client()
        except Exception:
            self.r = None

    def acquire(self) -> bool:
        if self.r is None:
            self.acquired = False
            return False

        try:
            ok = self.r.set(self.lock_key, "1", nx=True, ex=self.ttl_s)
            self.acquired = bool(ok)
            return self.acquired
        except Exception:
            self.acquired = False
            return False

    def release(self) -> None:
        if not self.acquired:
            return

        try:
            if self.r is not None:
                self.r.delete(self.lock_key)
        except Exception:
            pass
        finally:
            self.acquired = False


def wait_for_key(k: str, wait_s: float = LOCK_WAIT) -> Optional[Any]:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        v = cache_get_json(k)
        if v is not None:
            return v
        time.sleep(0.05)
    return None


def get_or_compute_json(
    *,
    cache_key: str,
    lock_name: str,
    ttl_s: int,
    compute_fn: Callable[[], Any],
    wait_s: float = LOCK_WAIT,
) -> Any:
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached

    lock = RedisLock(key_lock(lock_name), ttl_s=LOCK_TTL)

    if not lock.acquire():
        waited = wait_for_key(cache_key, wait_s=wait_s)
        if waited is not None:
            return waited

        data = compute_fn()
        cache_set_json(cache_key, data, ttl_s)
        return data

    try:
        cached2 = cache_get_json(cache_key)
        if cached2 is not None:
            return cached2

        data = compute_fn()
        cache_set_json(cache_key, data, ttl_s)
        return data
    finally:
        lock.release()


def get_or_compute_ev_report(set_code: str, kind: str, compute_fn: Callable[[], dict]) -> dict:
    """
    EV-specific wrapper. Includes kind in the cache key so WOE/box and
    WOE/draft_box do not collide.
    """
    code = set_code.strip().upper()
    k = kind.strip().lower()
    return get_or_compute_json(
        cache_key=key_ev(code, k),
        lock_name=f"ev:{code}:{k}",
        ttl_s=TTL_EV,
        compute_fn=compute_fn,
        wait_s=LOCK_WAIT,
    )
