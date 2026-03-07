# app/services/ebay_auth.py
from __future__ import annotations

import base64
import os

import requests

from app.services import ev_cache

SCOPE = "https://api.ebay.com/oauth/api_scope"

TOKEN_URL_PROD = "https://api.ebay.com/identity/v1/oauth2/token"
TOKEN_URL_SANDBOX = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"

EBAY_TOKEN_TTL = 6_600  # 110 minutes — eBay tokens live 120 min


def _env() -> str:
    return (os.getenv("EBAY_ENV", "production") or "production").strip().lower()


def _token_url() -> str:
    return TOKEN_URL_SANDBOX if _env() == "sandbox" else TOKEN_URL_PROD


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def _token_cache_key() -> str:
    return f"ebay:app_token:{_env()}"


def _fetch_fresh_token() -> str:
    """Hit eBay's OAuth endpoint and return a raw access token string."""
    client_id = (os.getenv("EBAY_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("EBAY_CLIENT_SECRET") or "").strip()

    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET in server environment"
        )

    url = _token_url()
    headers = {
        "Authorization": _basic_auth_header(client_id, client_secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials", "scope": SCOPE}

    try:
        r = requests.post(url, headers=headers, data=data, timeout=15)
        r.raise_for_status()
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        body = (getattr(e.response, "text", "") or "").strip()
        raise RuntimeError(
            f"eBay OAuth token request failed env={_env()} status={status} url={url} body={body[:1500]}"
        ) from e
    except requests.RequestException as e:
        raise RuntimeError(
            f"eBay OAuth token request failed (network) env={_env()} url={url} err={e}"
        ) from e

    payload = r.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(
            f"eBay token response missing access_token: {payload}"
        )

    return token


def get_app_access_token() -> str:
    """
    Return a valid eBay app access token.
    Served from Redis cache when available; fetches fresh and caches on miss.
    Falls back to a live fetch if Redis is unavailable.
    """
    key = _token_cache_key()

    cached = ev_cache.cache_get_json(key)
    if isinstance(cached, str) and cached:
        return cached

    token = _fetch_fresh_token()
    ev_cache.cache_set_json(key, token, EBAY_TOKEN_TTL)
    return token
