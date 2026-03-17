from __future__ import annotations

import base64
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from requests.exceptions import HTTPError, RequestException


# ============================================================
# Config
# ============================================================

EBAY_MARKETPLACE_ID = "EBAY_US"  # you chose EBAY-US
DEFAULT_LIMIT = 50
MAX_RESULTS_PER_PRODUCT = 200  # 4 pages of 50

# Environment:
#   sandbox => https://api.sandbox.ebay.com
#   prod    => https://api.ebay.com
EBAY_ENV = os.getenv("EBAY_ENV", "sandbox").strip().lower()
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID", "").strip()
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "").strip()

if EBAY_ENV not in ("sandbox", "prod", "production"):
    raise ValueError("EBAY_ENV must be 'sandbox' or 'prod' or 'production'")

API_BASE = "https://api.sandbox.ebay.com" if EBAY_ENV == "sandbox" else "https://api.ebay.com"
OAUTH_TOKEN_URL = f"{API_BASE}/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = f"{API_BASE}/buy/browse/v1/item_summary/search"

# Browse API typically uses this scope for client credentials
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"


# ============================================================
# Robust HTTP (retry + backoff)
# ============================================================

def _robust_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Any = None,
    timeout: int = 30,
    max_retries: int = 6,
) -> requests.Response:
    backoff = 0.5
    last_exc: Exception | None = None

    for _attempt in range(max_retries):
        try:
            r = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
                timeout=timeout,
            )

            # transient errors / throttling
            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        time.sleep(float(retry_after))
                        continue
                    except ValueError:
                        pass

                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue

            r.raise_for_status()
            return r

        except (HTTPError, RequestException) as e:
            last_exc = e
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)

    raise last_exc if last_exc else RuntimeError("Unknown request failure")


# ============================================================
# eBay OAuth: Application access token (client credentials)
# ============================================================

@dataclass
class OAuthToken:
    access_token: str
    expires_at_epoch: float  # epoch seconds


class EbayAuth:
    """
    Client-credentials token cache.
    """

    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise ValueError(
                "Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET env vars.")
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self._cached: Optional[OAuthToken] = None

    def get_token(self) -> str:
        now = time.time()
        if self._cached and now < (self._cached.expires_at_epoch - 60):
            return self._cached.access_token

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")

        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        # client credentials payload (form-encoded)
        payload = {
            "grant_type": "client_credentials",
            "scope": OAUTH_SCOPE,
        }
        data = "&".join(
            f"{k}={requests.utils.quote(str(v))}" for k, v in payload.items()
        )

        r = _robust_request("POST", OAUTH_TOKEN_URL,
                            headers=headers, data=data, timeout=30)
        j = r.json()
        token = j["access_token"]
        expires_in = float(j.get("expires_in", 7200))
        self._cached = OAuthToken(
            access_token=token, expires_at_epoch=now + expires_in)
        return token


# ============================================================
# Product definitions (easy to extend)
# ============================================================

@dataclass(frozen=True)
class ProductConfig:
    key: str                    # internal key: "WOE_SET_BOX"
    set_code: str               # "WOE"
    display_name: str           # "Wilds of Eldraine Set Booster Box"
    packs_per_box: int          # 30
    epid: Optional[str] = None
    gtin: Optional[str] = None
    upc_list: Tuple[str, ...] = ()
    mpn: Optional[str] = None

    required_tokens: Tuple[str, ...] = ()
    require_any_phrases: Tuple[str, ...] = ()
    exclude_tokens: Tuple[str, ...] = ()
    exclude_phrases: Tuple[str, ...] = ()


PRODUCTS: Dict[str, ProductConfig] = {
    "WOE_SET_BOX": ProductConfig(
        key="WOE_SET_BOX",
        set_code="WOE",
        display_name="Wilds of Eldraine Set Booster Box",
        packs_per_box=30,
        epid="10061983257",
        gtin="00195166231808",
        upc_list=("195166231808", "0195166231808"),
        mpn="D24680000",
        # matching rules
        required_tokens=("wilds", "eldraine", "booster", "box"),

        # require box phrasing
        require_any_phrases=("set booster box", "booster box",
                             "booster display", "display box", "set booster"),

        # Allow multi-box lots (do NOT exclude "lot")
        # Still exclude cases for now (you said cases later)
        exclude_tokens=(
            "collector", "draft", "bundle", "fat",
            "case", "single", "opened", "empty",
            # pack-only traps (often indicates non-box product lines)
            "omega", "blaster", "hanger",
            # explicit language signals (reject if present)
            "japanese", "japan", "jp", "ita", "italian", "deutsch", "german", "french", "francais",
            "spanish", "espanol", "italia", "korean", "china", "chinese",
            "neu", "fabrikversiegelt",
        ),
        exclude_phrases=(
            "fat pack", "collector booster", "draft booster", "bundle box",
            # single-pack phrasing (we still allow "30 packs" on boxes)
            "booster pack", "set booster pack",
        ),
    ),
}


# ============================================================
# Normalization + matching
# ============================================================

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_title(title: str) -> str:
    """
    Normalize for matching:
    - lowercase
    - normalize punctuation
    - collapse whitespace
    """
    t = (title or "").lower()
    t = t.replace("’", "'")
    t = re.sub(r"[\-–—_/|]+", " ", t)
    t = re.sub(r"[^a-z0-9\s']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokens(title_norm: str) -> set[str]:
    return set(_WORD_RE.findall(title_norm))


def contains_any_phrase(haystack_norm: str, phrases: Iterable[str]) -> bool:
    for p in phrases:
        if p and p in haystack_norm:
            return True
    return False


def looks_like_box_listing(tn: str, toks: set[str], *, packs_per_box: int) -> bool:
    """
    True if the title clearly indicates a full booster BOX.
    Excludes single booster packs, but allows box listings that mention "packs".
    """
    # Explicit box wording always wins
    if "set booster box" in tn or "booster box" in tn:
        return True

    # Display wording
    if "display" in toks:
        return True

    # Quantity-based inference (set booster boxes are typically packs_per_box packs)
    # Many listings say "30 packs" rather than "set booster box".
    if str(packs_per_box) in toks and "packs" in toks:
        return True

    # Multi-box wording
    if ("boxes" in toks or "box" in toks) and ("qty" in toks or "quantity" in toks):
        return True

    return False


def match_product_by_title(title: str, product: ProductConfig) -> Tuple[bool, str]:
    """
    Returns: (matched, reason)
    """
    if not title or len(title) > 1000:
        return (False, "invalid_title")
    tn = normalize_title(title)
    toks = tokens(tn)

    # hard excludes (phrases)
    for ex in product.exclude_phrases:
        if ex and ex in tn:
            return (False, f"excluded_phrase:{ex}")

    # hard excludes (tokens)
    for ex in product.exclude_tokens:
        if ex and ex in toks:
            return (False, f"excluded_token:{ex}")

    # required tokens
    missing = [rt for rt in product.required_tokens if rt not in toks]
    if missing:
        return (False, f"missing_required:{','.join(missing)}")

    # must contain one of the required phrases (e.g., "set booster box" or "booster box")
    if product.require_any_phrases and not contains_any_phrase(tn, product.require_any_phrases):
        return (False, "missing_required_phrase")

    # NEW: ensure it's actually a BOX listing (not single packs)
    if not looks_like_box_listing(tn, toks, packs_per_box=product.packs_per_box):
        return (False, "not_a_box_listing")

    # sealed bonus (optional)
    sealed_bonus = 0
    if "sealed" in toks or "factory sealed" in tn or "new sealed" in tn:
        sealed_bonus = 1

    packs_bonus = 0
    if str(product.packs_per_box) in toks and "packs" in toks:
        packs_bonus = 1

    reason = "title_match"
    if sealed_bonus or packs_bonus:
        reason += f":bonus(sealed={sealed_bonus},packs={packs_bonus})"
    return (True, reason)


def is_new_condition(cond: Optional[str], cond_id: Optional[str | int]) -> bool:
    """
    eBay conditionId 1000 == New.
    Some categories return condition text like "New/Factory Sealed".
    """
    if cond_id is not None:
        if str(cond_id).strip() == "1000":
            return True
        return False  # if conditionId exists and is not 1000, it's not new

    if cond is None:
        return False  # unknown

    c = str(cond).strip().lower()
    return c == "new" or c.startswith("new/")


# ============================================================
# Explain why items are rejected (debug)
# ============================================================

def _norm_simple(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def explain_reject_item(it: dict, product: ProductConfig, *, title_match: Tuple[bool, str]) -> List[str]:
    """
    Return a list of "reasons" an item would not be considered a match.
    Empty list => accept.
    """
    reasons: List[str] = []

    buying_opts = it.get("buyingOptions") or []
    if "FIXED_PRICE" not in buying_opts:
        reasons.append(f"no FIXED_PRICE (buyingOptions={buying_opts})")

    # Condition: filter out non-new when condition/conditionId is present.
    # Do NOT discard missing condition fields.
    cond = it.get("condition")
    cond_id = it.get("conditionId")
    if cond is not None or cond_id is not None:
        if not is_new_condition(cond, cond_id):
            reasons.append(
                f"not NEW (condition={cond!r}, conditionId={cond_id!r})")

    matched, reason = title_match
    if not matched:
        reasons.append(f"title_fail:{reason}")

    return reasons


def debug_print_rejections(
    items: List[dict],
    product: ProductConfig,
    *,
    max_print: int = 30,
) -> None:
    shown = 0
    for it in items:
        tm = match_product_by_title(it.get("title") or "", product)
        reasons = explain_reject_item(it, product, title_match=tm)
        if not reasons:
            continue

        print("\nREJECT:", (it.get("title") or "")[:140])
        print("  reasons:", "; ".join(reasons))
        print("  condition:", it.get("condition"),
              "conditionId:", it.get("conditionId"))
        print("  buyingOptions:", it.get("buyingOptions"))
        print("  categoryId:", it.get("categoryId"))
        print("  url:", it.get("itemWebUrl"))
        shown += 1
        if shown >= max_print:
            break


# ============================================================
# Browse search client
# ============================================================

@dataclass
class ListingRecord:
    listing_id: str
    title: str
    condition: Optional[str]
    condition_id: Optional[str]
    item_price: Optional[float]
    shipping_price: Optional[float]
    all_in_price: Optional[float]
    currency: Optional[str]
    end_time: Optional[str]
    url: Optional[str]
    category_id: Optional[str]
    search_path: str            # epid | gtin | keyword
    matched: bool
    match_reason: str
    quantity: int
    per_box_price: Optional[float]
    last_seen: str


class EbayBrowseClient:
    def __init__(self, auth: EbayAuth, marketplace_id: str = EBAY_MARKETPLACE_ID):
        self.auth = auth
        self.marketplace_id = marketplace_id

    def _headers(self) -> dict:
        token = self.auth.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
            "Accept": "application/json",
        }

    def search_item_summaries(
        self,
        *,
        epid: Optional[str] = None,
        gtin: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        filter_str: Optional[str] = None,
    ) -> dict:
        """
        Calls GET /buy/browse/v1/item_summary/search
        Notes:
        - You cannot use q with epid (Browse API rule).
        """
        params: Dict[str, Any] = {"limit": limit, "offset": offset}

        if epid:
            params["epid"] = epid
            if q:
                raise ValueError(
                    "Cannot use q with epid per eBay Browse API rules.")
        elif q:
            params["q"] = q

        if gtin:
            params["gtin"] = gtin

        if filter_str:
            params["filter"] = filter_str

        r = _robust_request(
            "GET",
            BROWSE_SEARCH_URL,
            headers=self._headers(),
            params=params,
            timeout=30,
        )
        return r.json()

    def iter_listings_for_product(
        self,
        product: ProductConfig,
        *,
        max_results: int = MAX_RESULTS_PER_PRODUCT,
        include_keyword_fallback: bool = True,
        debug_rejects: int = 0,
    ) -> List[ListingRecord]:
        """
        Tiered search:
          1) epid (if present)
          2) gtin (if present)
          3) keyword fallback (if enabled)

        Filters:
          - buyingOptions FIXED_PRICE (exclude auctions)
        NOTE:
          - We DO NOT filter conditions at the API level because some items omit condition fields.
            We'll enforce "New" only when condition/conditionId is present (in Python).
        """
        filter_str = "buyingOptions:{FIXED_PRICE}"

        all_items: Dict[str, ListingRecord] = {}
        raw_items_for_debug: List[dict] = []

        def _consume(payload: dict, search_path: str) -> None:
            items = payload.get("itemSummaries", []) or []
            now_iso = datetime.now(timezone.utc).isoformat()
            for it in items:
                raw_items_for_debug.append(it)
                rec = item_to_record(
                    it, product, search_path=search_path, seen_iso=now_iso)
                all_items[rec.listing_id] = rec

        def _paged(fetch_kwargs: dict, search_path: str) -> None:
            got = 0
            offset = 0
            while got < max_results:
                payload = self.search_item_summaries(
                    limit=min(DEFAULT_LIMIT, max_results - got),
                    offset=offset,
                    filter_str=filter_str,
                    **fetch_kwargs,
                )
                items = payload.get("itemSummaries", []) or []
                _consume(payload, search_path)
                n = len(items)
                got += n
                offset += n
                if n < DEFAULT_LIMIT:
                    break

        # Tier 1: ePID
        if product.epid:
            _paged({"epid": product.epid}, "epid")

        # Tier 2: GTIN
        if product.gtin:
            _paged({"gtin": product.gtin, "q": None}, "gtin")

        # Tier 3: keywords (broadened)
        if include_keyword_fallback:
            queries = [
                # broad
                "Wilds of Eldraine set booster box",
                "WOE set booster box",
                "MTG WOE set booster box",
                "Magic the Gathering Wilds of Eldraine set booster box",

                # include “display” phrasing
                "Wilds of Eldraine set booster display box",
                "WOE set booster display box",

                # identifiers (can appear in titles)
                product.mpn or "",
                *(product.upc_list or ()),
                product.gtin or "",
            ]
            queries = [q for q in queries if q.strip()]
            for q in queries:
                _paged({"q": q}, "keyword")

        records = list(all_items.values())

        if debug_rejects > 0:
            print(
                f"\n--- DEBUG: showing up to {debug_rejects} rejection reasons ---")
            debug_print_rejections(raw_items_for_debug,
                                   product, max_print=debug_rejects)
            print("--- END DEBUG ---\n")

        return records


# ============================================================
# Item parsing → ListingRecord
# ============================================================

def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _extract_price(it: dict) -> Tuple[Optional[float], Optional[str]]:
    p = it.get("price") or {}
    return _safe_float(p.get("value")), p.get("currency")


def _extract_shipping(it: dict) -> Optional[float]:
    opts = it.get("shippingOptions") or []
    if not opts:
        return None
    sc = (opts[0].get("shippingCost") or {})
    return _safe_float(sc.get("value"))


_QTY_PATTERNS: List[re.Pattern] = [
    re.compile(r"\blot of\s*(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*[x×]\b", re.I),          # 2x, 3×
    re.compile(r"\b(\d+)\s*boxes?\b", re.I),        # 2 boxes
    re.compile(r"\bqty\s*[:=]\s*(\d+)\b", re.I),    # qty: 2
]


def parse_quantity_from_title(title: str) -> int:
    t = title or ""
    for pat in _QTY_PATTERNS:
        m = pat.search(t)
        if m:
            return max(1, min(int(m.group(1)), 99))
    return 1


def item_to_record(it: dict, product: ProductConfig, *, search_path: str, seen_iso: str) -> ListingRecord:
    listing_id = it.get("itemId") or ""
    title = it.get("title") or ""
    cond = it.get("condition")
    cond_id = it.get("conditionId")
    end_time = it.get("itemEndDate")
    url = it.get("itemWebUrl")
    category_id = it.get("categoryId")  # may be missing; do NOT discard

    item_price, currency = _extract_price(it)
    shipping_price = _extract_shipping(it)

    all_in = None
    if item_price is not None and shipping_price is not None:
        all_in = item_price + shipping_price
    elif item_price is not None and shipping_price in (0.0, 0):
        all_in = item_price

    qty = parse_quantity_from_title(title)
    per_box = None
    if all_in is not None and qty > 0:
        per_box = all_in / float(qty)

    matched, reason = match_product_by_title(title, product)

    # Enforce NEW only if condition/conditionId is present.
    if cond is not None or cond_id is not None:
        if not is_new_condition(cond, cond_id):
            matched = False
            reason = f"non_new_condition:{cond}|{cond_id}"

    return ListingRecord(
        listing_id=listing_id,
        title=title,
        condition=cond,
        condition_id=str(cond_id) if cond_id is not None else None,
        item_price=item_price,
        shipping_price=shipping_price,
        all_in_price=all_in,
        currency=currency,
        end_time=end_time,
        url=url,
        category_id=category_id,
        search_path=search_path,
        matched=matched,
        match_reason=reason,
        last_seen=seen_iso,
        quantity=qty,
        per_box_price=per_box,
    )


# ============================================================
# Persistence (SQLite): current + optional price history
# ============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS listings_current (
  listing_id TEXT PRIMARY KEY,
  product_key TEXT NOT NULL,
  title TEXT,
  condition TEXT,
  condition_id TEXT,
  item_price REAL,
  shipping_price REAL,
  all_in_price REAL,
  currency TEXT,
  end_time TEXT,
  url TEXT,
  category_id TEXT,
  search_path TEXT,
  matched INTEGER,
  match_reason TEXT,
  first_seen TEXT,
  quantity INTEGER,
  per_box_price REAL,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS listings_price_history (
  listing_id TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  item_price REAL,
  shipping_price REAL,
  all_in_price REAL,
  currency TEXT,
  PRIMARY KEY (listing_id, seen_at)
);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    for stmt in SCHEMA_SQL.strip().split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s + ";")
    conn.commit()
    return conn


def ensure_migrations(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cols = {row[1] for row in cur.execute(
        "PRAGMA table_info(listings_current);").fetchall()}

    if "quantity" not in cols:
        cur.execute("ALTER TABLE listings_current ADD COLUMN quantity INTEGER;")
    if "per_box_price" not in cols:
        cur.execute(
            "ALTER TABLE listings_current ADD COLUMN per_box_price REAL;")

    conn.commit()


def upsert_listings(
    conn: sqlite3.Connection,
    product: ProductConfig,
    records: List[ListingRecord],
    *,
    write_history_on_price_change: bool = True,
) -> None:
    cur = conn.cursor()

    existing: Dict[str, Optional[float]] = {}
    if records:
        q = "SELECT listing_id, all_in_price FROM listings_current WHERE listing_id IN ({})".format(
            ",".join(["?"] * len(records))
        )
        for row in cur.execute(q, [r.listing_id for r in records]).fetchall():
            existing[row[0]] = row[1]

    now_iso = datetime.now(timezone.utc).isoformat()

    for r in records:
        first_seen = now_iso
        row = cur.execute(
            "SELECT first_seen FROM listings_current WHERE listing_id=?",
            (r.listing_id,),
        ).fetchone()
        if row and row[0]:
            first_seen = row[0]

        cur.execute(
            """
            INSERT INTO listings_current (
              listing_id, product_key, title, condition, condition_id,
              item_price, shipping_price, all_in_price, currency,
              end_time, url, category_id, search_path, matched, match_reason,
              first_seen, last_seen, quantity, per_box_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
              product_key=excluded.product_key,
              title=excluded.title,
              condition=excluded.condition,
              condition_id=excluded.condition_id,
              item_price=excluded.item_price,
              shipping_price=excluded.shipping_price,
              all_in_price=excluded.all_in_price,
              currency=excluded.currency,
              end_time=excluded.end_time,
              url=excluded.url,
              category_id=excluded.category_id,
              search_path=excluded.search_path,
              matched=excluded.matched,
              match_reason=excluded.match_reason,
              last_seen=excluded.last_seen,
              quantity=excluded.quantity,
              per_box_price=excluded.per_box_price
            """,
            (
                r.listing_id,
                product.key,
                r.title,
                r.condition,
                r.condition_id,
                r.item_price,
                r.shipping_price,
                r.all_in_price,
                r.currency,
                r.end_time,
                r.url,
                r.category_id,
                r.search_path,
                1 if r.matched else 0,
                r.match_reason,
                first_seen,
                r.last_seen,
                r.quantity,
                r.per_box_price,
            ),
        )

        if write_history_on_price_change:
            prev = existing.get(r.listing_id)
            if r.all_in_price is not None and (prev is None or abs(float(prev) - float(r.all_in_price)) > 0.01):
                cur.execute(
                    """
                    INSERT OR IGNORE INTO listings_price_history
                      (listing_id, seen_at, item_price, shipping_price, all_in_price, currency)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r.listing_id,
                        r.last_seen,
                        r.item_price,
                        r.shipping_price,
                        r.all_in_price,
                        r.currency,
                    ),
                )

    conn.commit()


# ============================================================
# Simple CLI demo
# ============================================================

def main():
    product = PRODUCTS["WOE_SET_BOX"]

    auth = EbayAuth(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET)
    client = EbayBrowseClient(auth, marketplace_id=EBAY_MARKETPLACE_ID)

    records = client.iter_listings_for_product(
        product,
        max_results=MAX_RESULTS_PER_PRODUCT,
        debug_rejects=30,
    )
    matched = [r for r in records if r.matched]

    print(f"EBAY_ENV={EBAY_ENV} Marketplace={EBAY_MARKETPLACE_ID}")
    print(
        f"Fetched {len(records)} listings; matched {len(matched)} for {product.display_name}\n")

    priced = [r for r in matched if r.per_box_price is not None]
    priced.sort(key=lambda x: x.per_box_price)  # type: ignore

    for r in priced[:10]:
        total = f"{r.all_in_price:.2f}" if r.all_in_price is not None else "?"
        print(
            f"- ${r.per_box_price:.2f}/box  (qty={r.quantity})  total=${total} | {r.condition} | {r.title}")
        print(f"  {r.url}")
        print(f"  search={r.search_path} reason={r.match_reason}\n")

    db_path = "ebay_listings.sqlite"
    conn = init_db(db_path)
    ensure_migrations(conn)
    upsert_listings(conn, product, records, write_history_on_price_change=True)
    print(f"Saved current + history to {db_path}")


if __name__ == "__main__":
    main()
