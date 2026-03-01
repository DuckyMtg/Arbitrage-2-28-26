# app/services/ebay_auth.py
from __future__ import annotations

import base64
import os

import requests


SCOPE = "https://api.ebay.com/oauth/api_scope"

TOKEN_URL_PROD = "https://api.ebay.com/identity/v1/oauth2/token"
TOKEN_URL_SANDBOX = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"


def _env() -> str:
    return (os.getenv("EBAY_ENV", "production") or "production").strip().lower()


def _token_url() -> str:
    return TOKEN_URL_SANDBOX if _env() == "sandbox" else TOKEN_URL_PROD


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    # Strip whitespace/newlines (very common .env issue)
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def get_app_access_token() -> str:
    client_id = (os.getenv("EBAY_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("EBAY_CLIENT_SECRET") or "").strip()

    print("RUNTIME CID:", repr(client_id))
    print("RUNTIME SEC:", repr(client_secret))

    if not client_id.strip() or not client_secret.strip():
        raise RuntimeError(
            "Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET in server environment")

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
            f"eBay OAuth token request failed (network) env={_env()} url={url} err={e}") from e

    payload = r.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(
            f"eBay token response missing access_token: {payload}")

    return token
