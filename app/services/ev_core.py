from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional, Union
from app.services import ev_cache

import requests
from requests.exceptions import HTTPError, RequestException

SCRYFALL_SET_URL = "https://api.scryfall.com/sets/{code}"
SCRYFALL_SEARCH_URL = "https://api.scryfall.com/cards/search"

HEADERS = {
    "User-Agent": "mtg-sealed-deals/0.5 (contact: 5.sided.die@gmail.com)",
    "Accept": "application/json",
}

# ----------------------------
# Robust HTTP
# ----------------------------


def scryfall_get(
    url: str,
    *,
    params: dict | None = None,
    timeout: int = 30,
    max_retries: int = 6,
):
    backoff = 0.5
    last_exc: Exception | None = None

    for _attempt in range(max_retries):
        try:
            r = requests.get(url, headers=HEADERS,
                             params=params, timeout=timeout)

            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        time.sleep(float(retry_after))
                        continue
                    except ValueError:
                        pass

                time.sleep(backoff + random.uniform(0, 0.25))
                backoff = min(backoff * 2, 8.0)
                continue

            r.raise_for_status()
            return r

        except (HTTPError, RequestException) as e:
            last_exc = e
            time.sleep(backoff + random.uniform(0, 0.25))
            backoff = min(backoff * 2, 8.0)

    raise last_exc if last_exc else RuntimeError(
        "Unknown Scryfall request failure")


def _q(*parts: str) -> str:
    return " ".join(p for p in parts if p and p.strip())


@lru_cache(maxsize=256)
def get_set_name(set_code: str) -> str:
    r = scryfall_get(SCRYFALL_SET_URL.format(code=set_code.lower()))
    return r.json().get("name", set_code.upper())


def _fetch_all_cards_uncached(query: str, *, unique: str = "cards", sleep_s: float = 0.12) -> list[dict]:
    params = {"q": query, "unique": unique, "order": "name"}
    url = SCRYFALL_SEARCH_URL
    out: list[dict] = []

    while True:
        try:
            r = scryfall_get(url, params=params if url ==
                             SCRYFALL_SEARCH_URL else None, timeout=30)
        except HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 404:
                return []
            raise

        payload = r.json()
        out.extend(payload.get("data", []))

        if not payload.get("has_more"):
            break

        url = payload["next_page"]
        params = None
        time.sleep(sleep_s)

    return out


def fetch_all_cards(query: str, *, unique: str = "cards") -> list[dict]:
    """
    Returns minimal card dicts: {"prices": {"usd": ..., "usd_foil": ...}}
    Cached in Redis for 7 days. Falls back to Scryfall on miss.
    """
    from app.services import ev_cache

    cache_key = ev_cache.key_cards(query, unique)
    cached = ev_cache.cache_get_json(cache_key)
    if cached is not None:
        return cached  # list of {"prices": {...}}

    full_cards = _fetch_all_cards_uncached(query, unique=unique)

    # Store only what we need — strip all Scryfall metadata
    minimal = [
        {"prices": c.get("prices") or {}}
        for c in full_cards
    ]

    ev_cache.cache_set_json(cache_key, minimal, ev_cache.TTL_CARDS)
    return minimal


def avg_price_usd(
    query: str,
    *,
    price_field: str = "usd",
    unique: str = "prints",
    warnings: list[str] | None = None,
) -> float:
    cards = fetch_all_cards(query, unique=unique)
    if not cards:
        return 0.0

    total = 0.0
    n_priced = 0
    n_total = len(cards)

    for c in cards:
        p = c.get("prices", {}).get(price_field)
        try:
            if p not in (None, ""):
                total += float(p)
                n_priced += 1
        except (TypeError, ValueError):
            pass  # skip unparseable prices too

    if n_priced == 0:
        return 0.0

    # Warn if significant data gap (< 80% of cards have prices)
    if warnings is not None and n_priced < n_total * 0.8:
        pct = int(100 * n_priced / n_total)
        warnings.append(
            f"avg_price_usd: only {n_priced}/{n_total} cards ({pct}%) "
            f"have a '{price_field}' price for query: {query!r}"
        )

    return total / n_priced


# ----------------------------
# Reporting
# ----------------------------
@dataclass
class PoolEval:
    label: str
    used_query: str
    count: int
    ev: float


@dataclass
class SlotEval:
    name: str
    ev: float
    pool_evals: list[PoolEval] = field(default_factory=list)


@dataclass
class EVReport:
    set_code: str
    set_name: str
    packs_per_box: int
    pack_ev: float
    box_ev: float
    slot_evals: list[SlotEval]
    warnings: list[str]
    counts: dict[str, int]  # pool-label -> count


# ----------------------------
# Pool primitives
# ----------------------------
@dataclass(frozen=True)
class QueryPool:
    """
    Prices cards returned by a Scryfall query.
    If primary returns 0 cards, optional fallback is used.
    """
    label: str
    primary: str
    fallback: Optional[str] = None
    unique: str = "prints"
    price_field: str = "usd"

    def eval(self, warnings: list[str]) -> PoolEval:
        cards_primary = fetch_all_cards(self.primary, unique=self.unique)
        if cards_primary:
            ev = avg_price_usd(
                self.primary, price_field=self.price_field, unique=self.unique, warnings=warnings)
            return PoolEval(self.label, self.primary, len(cards_primary), ev)

        if self.fallback:
            cards_fb = fetch_all_cards(self.fallback, unique=self.unique)
            if cards_fb:
                warnings.append(
                    f"[{self.label}] primary query returned 0 cards; used fallback.")
                ev = avg_price_usd(
                    self.fallback, price_field=self.price_field, unique=self.unique, warnings=warnings)
                return PoolEval(self.label, self.fallback, len(cards_fb), ev)

        warnings.append(
            f"[{self.label}] query returned 0 cards (no fallback or fallback empty).")
        return PoolEval(self.label, self.primary, 0, 0.0)


# ----------------------------
# Slot primitives
# ----------------------------
OutcomeValue = Union[float, QueryPool]


@dataclass(frozen=True)
class Slot:
    name: str
    outcomes: list[tuple[float, OutcomeValue]]
    strict_probs: bool = True
    tol: float = 1e-6
    renormalize: bool = False

    def eval(self, warnings: list[str], counts: dict[str, int]) -> SlotEval:
        total_p = sum(p for p, _ in self.outcomes)
        if total_p <= 0:
            warnings.append(
                f"[{self.name}] probability sum <= 0; slot EV forced to 0.")
            return SlotEval(self.name, 0.0, [])

        if self.strict_probs and abs(total_p - 1.0) > self.tol and not self.renormalize:
            warnings.append(
                f"[{self.name}] prob sum != 1.0 (got {total_p:.6f}). Using implicit renorm.")
            denom = total_p
        else:
            denom = total_p if (self.renormalize and abs(
                total_p - 1.0) > self.tol) else 1.0

        ev = 0.0
        pool_evals: list[PoolEval] = []

        for p, v in self.outcomes:
            w = p / denom
            if isinstance(v, (int, float)):
                ev += w * float(v)
            else:
                pe = v.eval(warnings)
                pool_evals.append(pe)
                counts[pe.label] = pe.count
                ev += w * pe.ev

        return SlotEval(self.name, ev, pool_evals)


@dataclass(frozen=True)
class ProductModel:
    set_code: str
    packs_per_box: int
    slots: list[Slot]

    def run(self) -> EVReport:
        warnings: list[str] = []
        counts: dict[str, int] = {}

        set_name = get_set_name(self.set_code)

        slot_evals: list[SlotEval] = []
        pack_ev = 0.0
        for s in self.slots:
            se = s.eval(warnings, counts)
            slot_evals.append(se)
            pack_ev += se.ev

        box_ev = pack_ev * self.packs_per_box

        return EVReport(
            set_code=self.set_code.upper(),
            set_name=set_name,
            packs_per_box=self.packs_per_box,
            pack_ev=pack_ev,
            box_ev=box_ev,
            slot_evals=slot_evals,
            warnings=warnings,
            counts=counts,
        )


# ----------------------------
# Shared helpers
# ----------------------------
DEFAULT_MYTHIC_RATE = 1 / 8


@lru_cache(maxsize=256)
def rarity_counts(set_code: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in ["common", "uncommon", "rare", "mythic"]:
        q = _q(f"set:{set_code}", f"rarity:{r}", "is:booster", "game:paper")
        counts[r] = len(fetch_all_cards(q, unique="cards"))
    return counts


def _normalize_exact_name(n: str) -> str:
    return n.replace("’", "'").strip()


def name_or_clause(names: list[str]) -> str:
    quoted = [f'"{_normalize_exact_name(n)}"' for n in names]
    return " or ".join(quoted)


def slot_any_rarity_from_set(
    *,
    slot_name: str,
    pool_label_prefix: str,
    set_code: str,
    price_field: str,
    unique: str = "prints",
) -> Slot:
    """
    Approximate "any rarity from a set" by weighting rarity averages
    by booster-eligible rarity counts.
    """
    c = rarity_counts(set_code)
    total = sum(c.get(r, 0) for r in ("common", "uncommon", "rare", "mythic"))
    if total <= 0:
        q = _q(f"set:{set_code}", "is:booster", "game:paper")
        return Slot(
            name=slot_name,
            outcomes=[(1.0, QueryPool(
                f"{pool_label_prefix}_any", q, unique=unique, price_field=price_field))],
            strict_probs=True,
        )

    outcomes: list[tuple[float, OutcomeValue]] = []
    for r in ("common", "uncommon", "rare", "mythic"):
        n = c.get(r, 0)
        if n <= 0:
            continue
        p = n / total
        q = _q(f"set:{set_code}", f"rarity:{r}", "is:booster", "game:paper")
        outcomes.append((p, QueryPool(
            f"{pool_label_prefix}_{r}", q, unique=unique, price_field=price_field)))

    return Slot(name=slot_name, outcomes=outcomes, strict_probs=True)

# ============================================================
# Config/Builder Pattern
# ============================================================


@dataclass
class RarityRates:
    """Probability weights for each rarity in a slot. Need not sum to 1."""
    common:   float = 0.0
    uncommon: float = 0.0
    rare:     float = 0.0
    mythic:   float = 0.0


@dataclass
class LandTypeConfig:
    """
    One category of land within the land slot.

    query_filters       extra Scryfall terms beyond 'set:xxx game:paper'
    rate                fraction of the land slot going to this category
    foil_rate           fraction of THIS category that is foil
    use_booster_filter  whether to add 'is:booster' to the primary query
    """
    label:              str
    query_filters:      list[str]
    rate:               float
    foil_rate:          float = 0.0
    unique:             str = "prints"
    use_booster_filter: bool = True


@dataclass
class PlayBoosterConfig:
    """
    Data-only description of a standard Play/Set Booster box.

    Pass to model_from_config() to build a ProductModel.
    For set-specific bonus slots (The List variant, dedicated bonus set, etc.)
    that cannot be expressed generically, build those Slots separately and
    pass them as the extra_slots argument to model_from_config().
    """
    set_code:       str
    packs_per_box:  int

    # --- Main R/M slot ---
    mythic_rate:          float = DEFAULT_MYTHIC_RATE
    # Fixed fraction of the main slot going to borderless treatments.
    # 0.0 means no borderless; non-zero splits both R and M by this fraction.
    borderless_fraction:  float = 0.0

    # --- Wildcard slot ---
    # Option A: supply explicit fixed rates per rarity
    wc_rates:         RarityRates | None = None
    # Option B: supply only the total RM rate; C/U split is derived from
    # rarity_counts() at model-build time.  Takes precedence over wc_rates.
    wc_rm_rate:       float | None = None
    wc_slots_per_pack: int = 1

    # --- Foil slot ---
    # Explicit rarity weights for the foil slot.
    # If None, weights are derived from rarity_counts() (card-count proportional).
    foil_rates:  RarityRates | None = None

    # --- Land slot ---
    # Leave empty if the set has no distinct land slot (e.g. MH3 land/common).
    land_types:  list[LandTypeConfig] = field(default_factory=list)

# ============================================================
# Generic slot builders
# ============================================================


def build_main_rm_slot(cfg: PlayBoosterConfig) -> Slot:
    """Standard main rare/mythic slot, with optional borderless split."""
    sc = cfg.set_code
    p_r = 1.0 - cfg.mythic_rate
    p_m = cfg.mythic_rate
    p_bl = cfg.borderless_fraction
    p_reg = 1.0 - p_bl

    if p_bl > 0:
        q_r = _q(f"set:{sc}", "rarity:rare",   "is:booster",
                 "game:paper", "-is:borderless")
        q_m = _q(f"set:{sc}", "rarity:mythic", "is:booster",
                 "game:paper", "-is:borderless")
        q_bl_r = _q(f"set:{sc}", "rarity:rare",
                    "is:borderless", "game:paper")
        q_bl_m = _q(f"set:{sc}", "rarity:mythic",
                    "is:borderless", "game:paper")
        outcomes = [
            (p_r * p_reg, QueryPool(f"{sc}_main_rare",
             q_r,    unique="prints", price_field="usd")),
            (p_r * p_bl,  QueryPool(f"{sc}_main_borderless_r",
             q_bl_r, unique="prints", price_field="usd")),
            (p_m * p_reg, QueryPool(f"{sc}_main_mythic",
             q_m,    unique="prints", price_field="usd")),
            (p_m * p_bl,  QueryPool(f"{sc}_main_borderless_m",
             q_bl_m, unique="prints", price_field="usd")),
        ]
    else:
        q_r = _q(f"set:{sc}", "rarity:rare",   "is:booster", "game:paper")
        q_m = _q(f"set:{sc}", "rarity:mythic", "is:booster", "game:paper")
        outcomes = [
            (p_r, QueryPool(f"{sc}_main_rare",   q_r,
             unique="prints", price_field="usd")),
            (p_m, QueryPool(f"{sc}_main_mythic", q_m,
             unique="prints", price_field="usd")),
        ]

    return Slot(name="Main R/M", outcomes=outcomes, strict_probs=True)


def build_wildcard_slot(cfg: PlayBoosterConfig) -> Slot:
    """Wildcard slot with fixed rates or C/U rates derived from card counts."""
    sc = cfg.set_code
    s = cfg.wc_slots_per_pack

    if cfg.wc_rm_rate is not None:
        c = rarity_counts(sc)
        cu = c.get("common", 0) + c.get("uncommon", 0)
        p_c = (c.get("common", 0) / cu) if cu else 0.5
        p_u = (c.get("uncommon", 0) / cu) if cu else 0.5
        rm = cfg.wc_rm_rate
        rates = RarityRates(
            common=(1 - rm) * p_c,
            uncommon=(1 - rm) * p_u,
            rare=rm * (1 - cfg.mythic_rate),
            mythic=rm * cfg.mythic_rate,
        )
    else:
        rates = cfg.wc_rates

    q_c = _q(f"set:{sc}", "rarity:common",   "is:booster", "game:paper")
    q_u = _q(f"set:{sc}", "rarity:uncommon", "is:booster", "game:paper")
    q_r = _q(f"set:{sc}", "rarity:rare",     "is:booster", "game:paper")
    q_m = _q(f"set:{sc}", "rarity:mythic",   "is:booster", "game:paper")

    return Slot(
        name=f"Wildcard ({s} slot{'s' if s > 1 else ''})",
        outcomes=[
            (s * rates.common,
             QueryPool(f"{sc}_wc_common",   q_c, unique="prints", price_field="usd")),
            (s * rates.uncommon,
             QueryPool(f"{sc}_wc_uncommon", q_u, unique="prints", price_field="usd")),
            (s * rates.rare,     QueryPool(f"{sc}_wc_rare",
             q_r, unique="prints", price_field="usd")),
            (s * rates.mythic,
             QueryPool(f"{sc}_wc_mythic",   q_m, unique="prints", price_field="usd")),
        ],
        strict_probs=True,
        renormalize=True,
    )


def build_foil_slot(cfg: PlayBoosterConfig) -> Slot:
    """Foil slot with explicit rarity weights or card-count-derived weights."""
    sc = cfg.set_code

    if cfg.foil_rates is None:
        c = rarity_counts(sc)
        total = sum(c.get(r, 0)
                    for r in ("common", "uncommon", "rare", "mythic"))
        rates = RarityRates(
            common=c.get("common",   0) / total if total else 0.60,
            uncommon=c.get("uncommon", 0) / total if total else 0.30,
            rare=c.get("rare",     0) / total if total else 0.08,
            mythic=c.get("mythic",   0) / total if total else 0.02,
        )
    else:
        rates = cfg.foil_rates

    def _pool(label: str, rarity: str) -> QueryPool:
        q = _q(f"set:{sc}", f"rarity:{rarity}",
               "is:booster", "game:paper", "finish:foil")
        q_fb = _q(f"set:{sc}", f"rarity:{rarity}", "is:booster", "game:paper")
        return QueryPool(f"{sc}_foil_{label}", q, fallback=q_fb,
                         unique="cards", price_field="usd_foil")

    return Slot(
        name="Traditional foil (rarity-weighted)",
        outcomes=[
            (rates.common,   _pool("c", "common")),
            (rates.uncommon, _pool("u", "uncommon")),
            (rates.rare,     _pool("r", "rare")),
            (rates.mythic,   _pool("m", "mythic")),
        ],
        strict_probs=True,
        renormalize=True,
    )


def build_land_slot(cfg: PlayBoosterConfig) -> Slot:
    """
    Land slot built from a list of LandTypeConfig entries.
    Each entry contributes nonfoil and/or foil outcomes at its stated rates.
    """
    sc = cfg.set_code
    outcomes: list[tuple[float, OutcomeValue]] = []

    for lt in cfg.land_types:
        booster_filter = ["is:booster"] if lt.use_booster_filter else []
        q = _q(f"set:{sc}", *lt.query_filters, *booster_filter, "game:paper")
        q_fb = _q(f"set:{sc}", *lt.query_filters, "game:paper")

        if lt.foil_rate < 1.0:
            outcomes.append((
                lt.rate * (1 - lt.foil_rate),
                QueryPool(f"{sc}_land_{lt.label}_nf", q, fallback=q_fb,
                          unique=lt.unique, price_field="usd"),
            ))
        if lt.foil_rate > 0.0:
            outcomes.append((
                lt.rate * lt.foil_rate,
                QueryPool(f"{sc}_land_{lt.label}_f", q, fallback=q_fb,
                          unique=lt.unique, price_field="usd_foil"),
            ))

    return Slot(name="Land slot", outcomes=outcomes, strict_probs=True)


def model_from_config(
    cfg: PlayBoosterConfig,
    extra_slots: list[Slot] | None = None,
) -> ProductModel:
    """
    Assemble a ProductModel from a PlayBoosterConfig.

    Builds main R/M, wildcard, foil, and land slots generically.
    Pass extra_slots for anything set-specific (bonus set, The List variant, etc.).
    """
    slots: list[Slot] = [
        build_main_rm_slot(cfg),
        build_wildcard_slot(cfg),
        build_foil_slot(cfg),
    ]
    if cfg.land_types:
        slots.append(build_land_slot(cfg))
    if extra_slots:
        slots.extend(extra_slots)

    return ProductModel(set_code=cfg.set_code, packs_per_box=cfg.packs_per_box, slots=slots)


# ============================================================
# ECL / TLA — shared config for treatment-slot play boosters
# ============================================================

@dataclass
class TreatmentPlayConfig:
    """
    Config for Play Booster sets that split their main/wildcard/foil slots
    into regular and treatment (high-CN) rare/mythic tiers, and replace
    one common per pack with a bonus sheet at a fixed rate.

    All p_* values are raw weights derived from the sheet; slots use
    renormalize=True so they need not sum to 1.0 exactly.
    """
    set_code:      str
    packs_per_box: int

    # Main R/M slot — total pool weight for each of the 4 tiers
    main_p_r:  float   # regular rare pool   (CN ≤ reg_rare_cn_max)
    main_p_m:  float   # regular mythic pool (CN ≤ reg_mythic_cn_max)
    main_p_tr: float   # treatment rare pool (CN ≥ treat_rare_cn_min)
    main_p_tm: float   # treatment mythic pool (CN ≥ treat_mythic_cn_min)

    # CN boundaries that separate regular from treatment printings
    reg_rare_cn_max:    int
    reg_mythic_cn_max:  int
    treat_rare_cn_min:  int
    treat_mythic_cn_min: int

    # Wildcard slot — total pool weights for 7 tiers
    wc_p_c:   float   # common
    wc_p_u:   float   # regular uncommon    (CN ≤ reg_uncommon_cn_max)
    wc_p_su:  float   # special uncommon    (CN ≥ special_u_cn_min)
    wc_p_r:   float   # regular rare
    wc_p_m:   float   # regular mythic
    wc_p_tr:  float   # treatment rare
    wc_p_tm:  float   # treatment mythic

    # CN boundary that separates regular from special uncommons
    reg_uncommon_cn_max: int
    special_u_cn_min:    int

    # Foil slot — same 7-tier structure as wildcard
    foil_p_c:  float
    foil_p_u:  float
    foil_p_su: float
    foil_p_r:  float
    foil_p_m:  float
    foil_p_tr: float
    foil_p_tm: float

    # Land slot — foil_rate fraction of the basic land draw is foil
    land_foil_rate: float = 0.20

    # Bonus slot (Special Guest / Source Material) replaces a common
    bonus_rate:       float = 0.0     # fraction of packs that get the bonus
    bonus_set:        str = ""      # Scryfall set code
    bonus_cn_min:     int = 0
    bonus_cn_max:     int = 0
    bonus_label:      str = ""
    bonus_slot_name:  str = ""


# ============================================================
# ECL config values
# ============================================================
# All weights computed directly from mtg.wtf sheet odds:
#   main/wildcard/foil tiers use (card_count × per-card-rate)

ECL_CONFIG = TreatmentPlayConfig(
    set_code="ecl",
    packs_per_box=36,

    # Main R/M (from rare_mythic_boosterfun sheet)
    main_p_r=70 * (459 / 38000),   # ≈ 0.8456
    main_p_m=24 * (459 / 76000),   # ≈ 0.1449
    main_p_tr=41 * (41 / 23500),   # ≈ 0.0716
    main_p_tm=24 * (41 / 47000),   # ≈ 0.0209

    reg_rare_cn_max=268,
    reg_mythic_cn_max=253,
    treat_rare_cn_min=297,
    treat_mythic_cn_min=284,

    # Wildcard (from wildcard sheet)
    wc_p_c=81 * (1 / 450),    # ≈ 0.1800
    wc_p_u=100 * (29 / 5000),    # ≈ 0.5800
    wc_p_su=10 * (9 / 7700),    # ≈ 0.0117  (fable uncommons)
    wc_p_r=70 * (19 / 6500),    # ≈ 0.2046
    wc_p_m=24 * (1 / 1100),    # ≈ 0.0218
    wc_p_tr=41 * (3 / 7700),    # ≈ 0.0160
    wc_p_tm=24 * (3 / 15400),    # ≈ 0.0047

    reg_uncommon_cn_max=263,
    special_u_cn_min=331,         # fable uncommons: CN 331-345

    # Foil (from foil sheet)
    foil_p_c=81 * (151 / 20250),   # ≈ 0.6044
    foil_p_u=100 * (149 / 50000),   # ≈ 0.2980
    foil_p_su=10 * (3 / 3500),   # ≈ 0.0086
    foil_p_r=70 * (1 / 1000),   # ≈ 0.0700
    foil_p_m=24 * (1 / 2000),   # ≈ 0.0120
    foil_p_tr=41 * (1 / 3500),   # ≈ 0.0117
    foil_p_tm=24 * (1 / 7000),   # ≈ 0.0034

    land_foil_rate=1 / 5,  # 55/275 packs get foil land

    bonus_rate=5 / 275,
    bonus_set="spg",
    bonus_cn_min=129,
    bonus_cn_max=148,
    bonus_label="ecl_spg",
    bonus_slot_name="Special Guests (SPG 129-148, replaces common)",
)

# ============================================================
# TLA config values
# ============================================================

TLA_CONFIG = TreatmentPlayConfig(
    set_code="tla",
    packs_per_box=36,

    # Main R/M (from rare_mythic_boosterfun sheet)
    main_p_r=62 * (463 / 35000),   # ≈ 0.8194
    main_p_m=26 * (463 / 70000),   # ≈ 0.1718
    main_p_tr=40 * (37 / 23500),   # ≈ 0.0630
    main_p_tm=28 * (37 / 47000),   # ≈ 0.0221

    reg_rare_cn_max=278,
    reg_mythic_cn_max=262,
    treat_rare_cn_min=302,
    treat_mythic_cn_min=297,

    # Wildcard (from wildcard sheet)
    wc_p_c=81 * (7 / 13500),    # ≈ 0.0420
    wc_p_u=110 * (741 / 110000),   # ≈ 0.7410
    wc_p_su=4 * (9 / 7375),    # ≈ 0.0049  (scene uncommons)
    wc_p_r=62 * (193 / 70000),    # ≈ 0.1710
    wc_p_m=26 * (193 / 140000),   # ≈ 0.0358
    wc_p_tr=40 * (3 / 7375),    # ≈ 0.0163
    wc_p_tm=28 * (3 / 14750),    # ≈ 0.0057

    reg_uncommon_cn_max=281,
    special_u_cn_min=299,         # scene uncommons: CN 299-306

    # Foil (from foil sheet)
    foil_p_c=81 * (539 / 80757),  # ≈ 0.5405
    foil_p_u=110 * (367 / 109670),  # ≈ 0.3680
    foil_p_su=4 * (36 / 58823),  # ≈ 0.0024
    foil_p_r=62 * (79 / 69790),  # ≈ 0.0702
    foil_p_m=26 * (79 / 139580),  # ≈ 0.0147
    foil_p_tr=40 * (12 / 58823),  # ≈ 0.0082
    foil_p_tm=28 * (6 / 58823),  # ≈ 0.0029

    land_foil_rate=1 / 5,  # 26/130 packs get foil land

    bonus_rate=5 / 130,
    bonus_set="tle",
    bonus_cn_min=1,
    bonus_cn_max=61,
    bonus_label="tla_source_material",
    bonus_slot_name="Source Material (TLE, replaces common)",
)

# ============================================================
# Generic slot builders for TreatmentPlayConfig
# ============================================================


def build_treatment_main_rm_slot(cfg: TreatmentPlayConfig) -> Slot:
    sc = cfg.set_code
    q_r = _q(f"set:{sc}", "rarity:rare",   f"cn<={cfg.reg_rare_cn_max}",
             "is:booster", "game:paper")
    q_m = _q(f"set:{sc}", "rarity:mythic", f"cn<={cfg.reg_mythic_cn_max}",
             "is:booster", "game:paper")
    q_tr = _q(f"set:{sc}", "rarity:rare",
              f"cn>={cfg.treat_rare_cn_min}",    "game:paper")
    q_tm = _q(f"set:{sc}", "rarity:mythic",
              f"cn>={cfg.treat_mythic_cn_min}",  "game:paper")
    return Slot(
        name="Main R/M (regular + treatment)",
        outcomes=[
            (cfg.main_p_r,  QueryPool(
                f"{sc}_main_r",  q_r,  unique="prints", price_field="usd")),
            (cfg.main_p_m,  QueryPool(
                f"{sc}_main_m",  q_m,  unique="prints", price_field="usd")),
            (cfg.main_p_tr, QueryPool(
                f"{sc}_main_tr", q_tr, unique="prints", price_field="usd")),
            (cfg.main_p_tm, QueryPool(
                f"{sc}_main_tm", q_tm, unique="prints", price_field="usd")),
        ],
        strict_probs=True,
        renormalize=True,
    )


def build_treatment_wildcard_slot(cfg: TreatmentPlayConfig) -> Slot:
    sc = cfg.set_code
    q_c = _q(f"set:{sc}", "rarity:common",   "is:booster", "game:paper")
    q_u = _q(f"set:{sc}", "rarity:uncommon",
             f"cn<={cfg.reg_uncommon_cn_max}", "game:paper")
    q_su = _q(f"set:{sc}", "rarity:uncommon",
              f"cn>={cfg.special_u_cn_min}",   "game:paper")
    q_r = _q(f"set:{sc}", "rarity:rare",
             f"cn<={cfg.reg_rare_cn_max}",    "is:booster", "game:paper")
    q_m = _q(f"set:{sc}", "rarity:mythic",
             f"cn<={cfg.reg_mythic_cn_max}",  "is:booster", "game:paper")
    q_tr = _q(f"set:{sc}", "rarity:rare",
              f"cn>={cfg.treat_rare_cn_min}",   "game:paper")
    q_tm = _q(f"set:{sc}", "rarity:mythic",
              f"cn>={cfg.treat_mythic_cn_min}", "game:paper")
    return Slot(
        name="Wildcard",
        outcomes=[
            (cfg.wc_p_c,  QueryPool(f"{sc}_wc_c",  q_c,
             unique="prints", price_field="usd")),
            (cfg.wc_p_u,  QueryPool(f"{sc}_wc_u",  q_u,
             unique="prints", price_field="usd")),
            (cfg.wc_p_su, QueryPool(
                f"{sc}_wc_su", q_su, unique="prints", price_field="usd")),
            (cfg.wc_p_r,  QueryPool(f"{sc}_wc_r",  q_r,
             unique="prints", price_field="usd")),
            (cfg.wc_p_m,  QueryPool(f"{sc}_wc_m",  q_m,
             unique="prints", price_field="usd")),
            (cfg.wc_p_tr, QueryPool(
                f"{sc}_wc_tr", q_tr, unique="prints", price_field="usd")),
            (cfg.wc_p_tm, QueryPool(
                f"{sc}_wc_tm", q_tm, unique="prints", price_field="usd")),
        ],
        strict_probs=True,
        renormalize=True,
    )


def build_treatment_foil_slot(cfg: TreatmentPlayConfig) -> Slot:
    sc = cfg.set_code

    def _fp(label: str, rarity: str, cn_filter: str = "", booster: bool = True) -> QueryPool:
        parts = [f"set:{sc}", f"rarity:{rarity}"]
        if cn_filter:
            parts.append(cn_filter)
        if booster:
            parts.append("is:booster")
        parts.append("game:paper")
        q = _q(*parts, "finish:foil")
        q_fb = _q(*parts)
        return QueryPool(f"{sc}_foil_{label}", q, fallback=q_fb,
                         unique="cards", price_field="usd_foil")

    return Slot(
        name="Traditional foil",
        outcomes=[
            (cfg.foil_p_c,  _fp("c",  "common")),
            (cfg.foil_p_u,  _fp("u",  "uncommon",
             f"cn<={cfg.reg_uncommon_cn_max}", booster=False)),
            (cfg.foil_p_su, _fp("su", "uncommon",
             f"cn>={cfg.special_u_cn_min}",   booster=False)),
            (cfg.foil_p_r,  _fp("r",  "rare",
             f"cn<={cfg.reg_rare_cn_max}")),
            (cfg.foil_p_m,  _fp("m",  "mythic",
             f"cn<={cfg.reg_mythic_cn_max}")),
            (cfg.foil_p_tr, _fp("tr", "rare",
             f"cn>={cfg.treat_rare_cn_min}",   booster=False)),
            (cfg.foil_p_tm, _fp("tm", "mythic",
             f"cn>={cfg.treat_mythic_cn_min}", booster=False)),
        ],
        strict_probs=True,
        renormalize=True,
    )


def build_treatment_land_slot(cfg: TreatmentPlayConfig) -> Slot:
    sc = cfg.set_code
    q = _q(f"set:{sc}", "type:basic", "is:booster", "game:paper")
    q_fb = _q(f"set:{sc}", "type:basic", "game:paper")
    return Slot(
        name="Land",
        outcomes=[
            (1.0 - cfg.land_foil_rate,
             QueryPool(f"{sc}_land_nf", q, fallback=q_fb, unique="prints", price_field="usd")),
            (cfg.land_foil_rate,
             QueryPool(f"{sc}_land_f",  q, fallback=q_fb, unique="prints", price_field="usd_foil")),
        ],
        strict_probs=True,
    )


def build_treatment_bonus_slot(cfg: TreatmentPlayConfig) -> Slot:
    """Bonus sheet that replaces one common at cfg.bonus_rate."""
    q = _q(f"set:{cfg.bonus_set}",
           f"cn>={cfg.bonus_cn_min}", f"cn<={cfg.bonus_cn_max}", "game:paper")
    return Slot(
        name=cfg.bonus_slot_name,
        outcomes=[
            (1.0 - cfg.bonus_rate, 0.0),
            (cfg.bonus_rate, QueryPool(cfg.bonus_label, q, fallback=q,
                                       unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_from_treatment_config(cfg: TreatmentPlayConfig) -> ProductModel:
    return ProductModel(
        set_code=cfg.set_code,
        packs_per_box=cfg.packs_per_box,
        slots=[
            build_treatment_main_rm_slot(cfg),
            build_treatment_wildcard_slot(cfg),
            build_treatment_foil_slot(cfg),
            build_treatment_land_slot(cfg),
            build_treatment_bonus_slot(cfg),
        ],
    )


def model_ecl_play_box() -> ProductModel:
    return model_from_treatment_config(ECL_CONFIG)


def model_tla_play_box() -> ProductModel:
    return model_from_treatment_config(TLA_CONFIG)


# ============================================================
# WOE Draft Booster — uses PlayBoosterConfig + extra_slots
# ============================================================
# Pack variants: 2/3 have no foil; 1/3 replace a common with foil_with_showcase.
# WOT (Enchanting Tales) is always present as a dedicated slot.
# No wildcard slot; no land slot (basic folded into 9 commons).

_WOT_DRAFT_P_U = 18 * (4 / 147) + 25 * (2 / 147)  # ≈ 0.8299  (all uncommons)
# ≈ 0.1276  (regular rares, CN≤63)
_WOT_DRAFT_P_R = 5 * (1 / 98) + 15 * (1 / 196)
# ≈ 0.0170  (anime rares, CN≥64)
_WOT_DRAFT_P_AR = 5 * (1 / 294)
# ≈ 0.0255  (anime mythics)
_WOT_DRAFT_P_AM = 15 * (1 / 588)


def slot_woe_draft_enchanting_tales() -> Slot:
    q_u = _q("set:wot", "rarity:uncommon", "cn<=63", "game:paper")
    q_r = _q("set:wot", "rarity:rare",     "cn<=63", "game:paper")
    q_ar = _q("set:wot", "rarity:rare",     "cn>=64", "game:paper")
    q_am = _q("set:wot", "rarity:mythic",             "game:paper")
    return Slot(
        name="Enchanting Tales / WOT (always present)",
        outcomes=[
            (_WOT_DRAFT_P_U,  QueryPool("woe_draft_wot_u",
             q_u,  unique="prints", price_field="usd")),
            (_WOT_DRAFT_P_R,  QueryPool("woe_draft_wot_r",
             q_r,  unique="prints", price_field="usd")),
            (_WOT_DRAFT_P_AR, QueryPool("woe_draft_wot_ar",
             q_ar, unique="prints", price_field="usd")),
            (_WOT_DRAFT_P_AM, QueryPool("woe_draft_wot_am",
             q_am, unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def slot_woe_draft_foil() -> Slot:
    """
    foil_with_showcase replaces a common in 1/3 packs.
    Foil commons/basics (≈60% of the foil sheet) have EV≈$0 and are
    folded into the zero outcome alongside the 2/3 no-foil packs.
    From the foil_with_showcase sheet:
      uncommon : 25.0%  (98 cards at 1/392)
      rare     : 12.1%
      mythic   :  2.97%
      common   : 60.0%  → folded to zero
    """
    p_foil = 1 / 3
    _p_fu = 0.2500
    _p_fr = 0.1210
    _p_fm = 0.0297

    def _fp(label: str, rarity: str) -> QueryPool:
        q = _q("(set:woe or set:wot)",
               f"rarity:{rarity}", "finish:foil", "game:paper")
        q_fb = _q("(set:woe or set:wot)", f"rarity:{rarity}", "game:paper")
        return QueryPool(f"woe_draft_foil_{label}", q, fallback=q_fb,
                         unique="cards", price_field="usd_foil")

    return Slot(
        name="Foil (replaces common, 1/3 packs)",
        outcomes=[
            (1 - p_foil * (_p_fu + _p_fr + _p_fm), 0.0),
            (p_foil * _p_fu, _fp("u", "uncommon")),
            (p_foil * _p_fr, _fp("r", "rare")),
            (p_foil * _p_fm, _fp("m", "mythic")),
        ],
        strict_probs=True,
        renormalize=True,
    )


WOE_DRAFT_CONFIG = PlayBoosterConfig(
    set_code="woe",
    packs_per_box=36,
    # Draft boosters have no wildcard, no dedicated land slot, no main foil slot —
    # all handled by the extra_slots below; the generic builders are unused.
    # We set mythic_rate only so build_main_rm_slot has a value; it's never called.
    mythic_rate=20 / 138,
    wc_rates=RarityRates(),  # zeroed — no wildcard slot in draft boosters
    wc_slots_per_pack=0,     # suppresses wildcard slot in model_from_config
)


def model_woe_draft_box() -> ProductModel:
    # Draft boosters don't have a generic wildcard or foil slot;
    # build them explicitly and bypass the generic builders entirely.
    slots = [
        build_main_rm_slot(WOE_DRAFT_CONFIG),
        slot_woe_draft_enchanting_tales(),
        slot_woe_draft_foil(),
    ]
    return ProductModel(
        set_code=WOE_DRAFT_CONFIG.set_code,
        packs_per_box=WOE_DRAFT_CONFIG.packs_per_box,
        slots=slots,
    )

# ============================================================
# OTJ
# ============================================================


OTJ_CONFIG = PlayBoosterConfig(
    set_code="otj",
    packs_per_box=36,
    mythic_rate=DEFAULT_MYTHIC_RATE,
    borderless_fraction=0.40,      # regular 60% / borderless 40%
    wc_rm_rate=1 / 12,      # C/U split derived from card counts
    # Foil: None → computed from card counts
    land_types=[
        LandTypeConfig("dual",  ["type:land", "rarity:common",
                       "-type:basic"], rate=1/2, foil_rate=1/5),
        LandTypeConfig("west",  ["type:basic", "is:fullart"],
                       rate=1/6, foil_rate=1/5),
        LandTypeConfig("basic", ["type:basic", "-is:fullart"],
                       rate=1/3, foil_rate=1/5),
    ],
)

# Breaking News and The List are kept as custom functions because each
# involves multiple pools at different rates that don't fit a single template.


def slot_otj_breaking_news() -> Slot:
    # OTP sheet: 80 uncommons, 60 rares, 15 mythics out of 155 total
    p_u = 80 / 155
    p_r = 60 / 155
    p_m = 15 / 155

    q_u = _q("set:otp", "rarity:uncommon", "is:booster", "game:paper")
    q_r = _q("set:otp", "rarity:rare",     "is:booster", "game:paper")
    q_m = _q("set:otp", "rarity:mythic",   "is:booster", "game:paper")

    return Slot(
        name="OTP (Breaking News) dedicated slot",
        outcomes=[
            (p_u, QueryPool("otp_u", q_u, fallback=_q(
                "set:otp", "rarity:uncommon", "game:paper"), unique="prints", price_field="usd")),
            (p_r, QueryPool("otp_r", q_r, fallback=_q("set:otp", "rarity:rare",
             "game:paper"), unique="prints", price_field="usd")),
            (p_m, QueryPool("otp_m", q_m, fallback=_q("set:otp", "rarity:mythic",
             "game:paper"), unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def slot_otj_the_list() -> Slot:
    p_list = 1 / 5
    p_spg = 1 / 64
    p_big = max(0.0, p_list - p_spg)

    q_big = _q("set:big", "game:paper")
    q_spg = _q("set:spg", "cn>=29", "cn<=38", "game:paper")

    return Slot(
        name="The List (BIG/SPG) replaces a common",
        outcomes=[
            (1.0 - p_list, 0.0),
            (p_big, QueryPool("otj_list_big", q_big, fallback=q_big,
             unique="cards",  price_field="usd")),
            (p_spg, QueryPool("otj_list_spg", q_spg, fallback=_q(
                "set:spg", "game:paper"),    unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_otj_play_box() -> ProductModel:
    return model_from_config(
        OTJ_CONFIG,
        extra_slots=[slot_otj_breaking_news(), slot_otj_the_list()],
    )


# ============================================================
# WOE
# ============================================================

WOE_CONFIG = PlayBoosterConfig(
    set_code="woe",
    packs_per_box=30,
    mythic_rate=20 / 138,
    # No borderless in main slot
    wc_rates=RarityRates(common=0.700, uncommon=0.168,
                         rare=0.095, mythic=0.025),
    wc_slots_per_pack=2,
    foil_rates=RarityRates(common=0.595, uncommon=0.247,
                           rare=0.120, mythic=0.038),
    land_types=[
        LandTypeConfig(
            "regular",  ["type:basic", "-frame:showcase"], rate=0.67, foil_rate=0.20),
        LandTypeConfig(
            "showcase", ["type:basic",  "frame:showcase"], rate=0.33, foil_rate=0.20),
    ],
)

# Enchanting Tales rates — kept at module level for documentation clarity
_WOT_ANIME_RARE_RATE = 5 / 249
_WOT_ANIME_MYTHIC_RATE = 15 / 588
_WOT_RARITY_WEIGHTS = {"uncommon": 72 / 147,
                       "rare": 60 / 147, "mythic": 15 / 147}

_WOT_ANIME_RARES = [
    "Aggravated Assault", "Land Tax", "Necropotence", "Primal Vigor", "Rhystic Study",
]
_WOT_ANIME_MYTHICS = [
    "Smothering Tithe", "Doubling Season", "Omniscience", "Beseech the Mirror",
    "Defense of the Heart", "Greater Auramancy", "Kindred Discovery", "Leyline of the Void",
    "Parallel Lives", "Sneak Attack", "Spreading Plague", "The Great Henge",
    "Utopia Sprawl", "Waste Not", "Wound Reflection",
]


def slot_woe_enchanting_tales() -> Slot:
    p_anime_r = _WOT_ANIME_RARE_RATE
    p_anime_m = _WOT_ANIME_MYTHIC_RATE
    p_normal = max(0.0, 1.0 - p_anime_r - p_anime_m)
    w = _WOT_RARITY_WEIGHTS

    q_u = _q("set:wot", "rarity:uncommon", "finish:nonfoil", "game:paper")
    q_r = _q("set:wot", "rarity:rare",     "finish:nonfoil", "game:paper")
    q_m = _q("set:wot", "rarity:mythic",   "finish:nonfoil", "game:paper")

    q_anime_r = _q(f"({name_or_clause(_WOT_ANIME_RARES)})",
                   "set:wot", "finish:nonfoil", "game:paper")
    q_anime_m = _q(f"({name_or_clause(_WOT_ANIME_MYTHICS)})",
                   "set:wot", "finish:nonfoil", "game:paper")

    return Slot(
        name="Enchanting Tales (dedicated)",
        outcomes=[
            (p_normal * w["uncommon"], QueryPool("wot_u",
             q_u,       unique="prints", price_field="usd")),
            (p_normal * w["rare"],     QueryPool("wot_r",
             q_r,       unique="prints", price_field="usd")),
            (p_normal * w["mythic"],   QueryPool("wot_m",
             q_m,       unique="prints", price_field="usd")),
            (p_anime_r,                QueryPool("wot_anime_r",
             q_anime_r, unique="prints", price_field="usd")),
            (p_anime_m,                QueryPool("wot_anime_m",
             q_anime_m, unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def slot_woe_the_list() -> Slot:
    p_list = 0.25
    q_plst = _q("set:plst", "is:booster", "game:paper")
    return Slot(
        name="The List (approx via PLST)",
        outcomes=[
            (1.0 - p_list, 0.0),
            (p_list, QueryPool("woe_list_plst", q_plst,
                               fallback=_q("set:plst", "game:paper"),
                               unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_woe_set_box() -> ProductModel:
    return model_from_config(
        WOE_CONFIG,
        extra_slots=[slot_woe_enchanting_tales(), slot_woe_the_list()],
    )


# ============================================================
# MH3
# ============================================================

@dataclass
class MH3Config:
    set_code:      str = "mh3"
    packs_per_box: int = 36

    # Main R/M slot — WotC published totals
    main_p_r:               float = 0.798
    main_p_m:               float = 0.130
    main_p_retro_total:     float = 0.021   # 24R : 8M = 3:1
    main_p_borderless_total: float = 0.051
    main_retro_r_count:     int = 24      # used to split retro R vs M
    main_retro_m_count:     int = 8

    # New-to-Modern slot — WotC published totals
    ntm_p_u:                     float = 0.750
    ntm_p_r:                     float = 0.213
    ntm_p_m:                     float = 0.023
    ntm_p_framebreak_total:      float = 0.008   # 6R + 1M
    ntm_p_profile_total:         float = 0.003   # 2R + 2M
    ntm_p_retro_total:           float = 0.002   # 2R + 1M
    ntm_p_extra_borderless_mythic: float = 0.0005

    # Wildcard slot — WotC published totals
    wc_p_c:             float = 0.417
    wc_p_u:             float = 0.334
    wc_p_dfc_u:         float = 0.083
    wc_p_r:             float = 0.067
    wc_p_m:             float = 0.011
    wc_p_borderless_rm: float = 0.004
    wc_p_retro:         float = 0.042
    wc_p_cmdr_mythic:   float = 0.042
    wc_p_snow_wastes:   float = 0.0005

    # Land/common slot — WotC published totals
    lc_p_common:           float = 0.50
    lc_p_basic_nf:         float = 0.20
    lc_p_basic_f:          float = 0.133
    lc_p_eldrazi_basic_nf: float = 0.10
    lc_p_eldrazi_basic_f:  float = 0.067

    # Special Guest replacement
    special_guest_rate: float = 1 / 64


MH3 = MH3Config()   # singleton — import and use this everywhere below


def slot_mh3_main_rm(cfg: MH3Config = MH3) -> Slot:
    sc = cfg.set_code
    retro_total = cfg.main_retro_r_count + cfg.main_retro_m_count
    p_retro_r = cfg.main_p_retro_total * (cfg.main_retro_r_count / retro_total)
    p_retro_m = cfg.main_p_retro_total * (cfg.main_retro_m_count / retro_total)

    q_r_reg = _q(f"set:{sc}", "rarity:rare",   "is:booster",
                 "game:paper", "-frame:1997", "-is:borderless")
    q_m_reg = _q(f"set:{sc}", "rarity:mythic", "is:booster",
                 "game:paper", "-frame:1997", "-is:borderless")
    q_r_reg_fb = _q(f"set:{sc}", "rarity:rare",
                    "game:paper", "-frame:1997", "-is:borderless")
    q_m_reg_fb = _q(f"set:{sc}", "rarity:mythic",
                    "game:paper", "-frame:1997", "-is:borderless")

    q_r_retro = _q(f"set:{sc}", "frame:1997",
                   "rarity:rare",   "is:booster", "game:paper")
    q_m_retro = _q(f"set:{sc}", "frame:1997",
                   "rarity:mythic", "is:booster", "game:paper")
    q_r_retro_fb = _q(f"set:{sc}", "frame:1997", "rarity:rare",   "game:paper")
    q_m_retro_fb = _q(f"set:{sc}", "frame:1997", "rarity:mythic", "game:paper")

    q_bl_r = _q(f"set:{sc}", "is:borderless", "rarity:rare",   "game:paper")
    q_bl_m = _q(f"set:{sc}", "is:borderless", "rarity:mythic", "game:paper")
    br = len(fetch_all_cards(q_bl_r, unique="cards"))
    bm = len(fetch_all_cards(q_bl_m, unique="cards"))
    bt = br + bm
    p_bl_r = (br / bt) if bt else 0.5
    p_bl_m = (bm / bt) if bt else 0.5

    return Slot(
        name="Main R/M (incl. retro + borderless)",
        outcomes=[
            (cfg.main_p_r,                            QueryPool("mh3_main_regular_r",
             q_r_reg,    fallback=q_r_reg_fb,  unique="prints", price_field="usd")),
            (cfg.main_p_m,                            QueryPool("mh3_main_regular_m",
             q_m_reg,    fallback=q_m_reg_fb,  unique="prints", price_field="usd")),
            (p_retro_r,                               QueryPool("mh3_main_retro_r",
             q_r_retro,  fallback=q_r_retro_fb, unique="prints", price_field="usd")),
            (p_retro_m,                               QueryPool("mh3_main_retro_m",
             q_m_retro,  fallback=q_m_retro_fb, unique="prints", price_field="usd")),
            (cfg.main_p_borderless_total * p_bl_r,   QueryPool("mh3_main_borderless_r",
             q_bl_r,   fallback=q_bl_r,      unique="prints", price_field="usd")),
            (cfg.main_p_borderless_total * p_bl_m,   QueryPool("mh3_main_borderless_m",
             q_bl_m,   fallback=q_bl_m,      unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def slot_mh3_new_to_modern(cfg: MH3Config = MH3) -> Slot:
    sc = cfg.set_code

    p_retro_r_m = cfg.ntm_p_retro_total
    p_bl_r = (cfg.ntm_p_framebreak_total * (6/7)) + \
        (cfg.ntm_p_profile_total * 0.5)
    p_bl_m = (cfg.ntm_p_framebreak_total * (1/7)) + \
        (cfg.ntm_p_profile_total * 0.5) + cfg.ntm_p_extra_borderless_mythic

    q_u_reg = _q(f"set:{sc}", "-is:reprint", "rarity:uncommon",
                 "is:booster", "game:paper", "-frame:1997", "-is:borderless")
    q_r_reg = _q(f"set:{sc}", "-is:reprint", "rarity:rare",
                 "is:booster", "game:paper", "-frame:1997", "-is:borderless")
    q_m_reg = _q(f"set:{sc}", "-is:reprint", "rarity:mythic",
                 "is:booster", "game:paper", "-frame:1997", "-is:borderless")
    q_u_reg_fb = _q(f"set:{sc}", "-is:reprint", "rarity:uncommon",
                    "game:paper", "-frame:1997", "-is:borderless")
    q_r_reg_fb = _q(f"set:{sc}", "-is:reprint", "rarity:rare",
                    "game:paper", "-frame:1997", "-is:borderless")
    q_m_reg_fb = _q(f"set:{sc}", "-is:reprint", "rarity:mythic",
                    "game:paper", "-frame:1997", "-is:borderless")

    q_retro_any = _q(f"set:{sc}", "-is:reprint", "frame:1997",
                     "(rarity:rare or rarity:mythic)", "is:booster", "game:paper")
    q_retro_any_fb = _q(f"set:{sc}", "-is:reprint", "frame:1997",
                        "(rarity:rare or rarity:mythic)", "game:paper")
    q_bl_any = _q(f"set:{sc}", "-is:reprint", "is:borderless",
                  "(rarity:rare or rarity:mythic)", "game:paper")
    q_bl_m = _q(f"set:{sc}", "-is:reprint", "is:borderless",
                "rarity:mythic",                  "game:paper")

    return Slot(
        name="New-to-Modern slot (approx via -is:reprint)",
        outcomes=[
            (cfg.ntm_p_u, QueryPool("mh3_ntm_u_reg",        q_u_reg,
             fallback=q_u_reg_fb,     unique="prints", price_field="usd")),
            (cfg.ntm_p_r, QueryPool("mh3_ntm_r_reg",        q_r_reg,
             fallback=q_r_reg_fb,     unique="prints", price_field="usd")),
            (cfg.ntm_p_m, QueryPool("mh3_ntm_m_reg",        q_m_reg,
             fallback=q_m_reg_fb,     unique="prints", price_field="usd")),
            (p_retro_r_m, QueryPool("mh3_ntm_retro_any",    q_retro_any,
             fallback=q_retro_any_fb, unique="prints", price_field="usd")),
            (p_bl_r,      QueryPool("mh3_ntm_borderless_any", q_bl_any,
             fallback=q_bl_any,       unique="prints", price_field="usd")),
            (p_bl_m,      QueryPool("mh3_ntm_borderless_m",   q_bl_m,
             fallback=q_bl_m,         unique="prints", price_field="usd")),
        ],
        strict_probs=True,
        renormalize=True,
    )


def slot_mh3_wildcard(cfg: MH3Config = MH3) -> Slot:
    sc = cfg.set_code

    def _q_pair(rarity: str, *extra: str):
        return (
            _q(f"set:{sc}", f"rarity:{rarity}",
               "is:booster", "game:paper", *extra),
            _q(f"set:{sc}", f"rarity:{rarity}", "game:paper", *extra),
        )

    q_c,    q_c_fb = _q_pair("common")
    q_u,    q_u_fb = _q_pair("uncommon")
    q_dfc,  q_dfc_fb = _q_pair("uncommon", "is:dfc")
    q_r,    q_r_fb = _q_pair("rare")
    q_m,    q_m_fb = _q_pair("mythic")

    q_bl_rm = _q(f"set:{sc}", "is:borderless",
                 "(rarity:rare or rarity:mythic)", "is:booster", "game:paper")
    q_bl_rm_fb = _q(f"set:{sc}", "is:borderless",
                    "(rarity:rare or rarity:mythic)", "game:paper")
    q_retro = _q(f"set:{sc}", "frame:1997", "is:booster", "game:paper")
    q_retro_fb = _q(f"set:{sc}", "frame:1997", "game:paper")
    q_cmdr = _q("set:m3c", "rarity:mythic", "game:paper")
    q_snow = _q(f"set:{sc}", 'name:"Snow-Covered Wastes"', "game:paper")

    return Slot(
        name="Wildcard (any rarity)",
        outcomes=[
            (cfg.wc_p_c,             QueryPool("mh3_wc_c",            q_c,
             fallback=q_c_fb,    unique="prints", price_field="usd")),
            (cfg.wc_p_u,             QueryPool("mh3_wc_u",            q_u,
             fallback=q_u_fb,    unique="prints", price_field="usd")),
            (cfg.wc_p_dfc_u,         QueryPool("mh3_wc_dfc_u",        q_dfc,
             fallback=q_dfc_fb,  unique="prints", price_field="usd")),
            (cfg.wc_p_r,             QueryPool("mh3_wc_r",            q_r,
             fallback=q_r_fb,    unique="prints", price_field="usd")),
            (cfg.wc_p_m,             QueryPool("mh3_wc_m",            q_m,
             fallback=q_m_fb,    unique="prints", price_field="usd")),
            (cfg.wc_p_borderless_rm, QueryPool("mh3_wc_borderless_rm", q_bl_rm,
             fallback=q_bl_rm_fb, unique="prints", price_field="usd")),
            (cfg.wc_p_retro,         QueryPool("mh3_wc_retro_any",    q_retro,
             fallback=q_retro_fb, unique="prints", price_field="usd")),
            (cfg.wc_p_cmdr_mythic,   QueryPool("mh3_wc_cmdr_mythic",  q_cmdr,
             fallback=q_cmdr,    unique="prints", price_field="usd")),
            (cfg.wc_p_snow_wastes,   QueryPool("mh3_wc_snow_wastes",  q_snow,
             fallback=q_snow,    unique="prints", price_field="usd")),
        ],
        strict_probs=True,
        renormalize=True,
    )


def slot_mh3_traditional_foil(cfg: MH3Config = MH3) -> Slot:
    sc = cfg.set_code

    def _pair(rarity: str, *extra: str):
        q = _q(f"set:{sc}", f"rarity:{rarity}", "is:booster",
               "game:paper", "finish:foil", *extra)
        q_fb = _q(f"set:{sc}", f"rarity:{rarity}",
                  "is:booster", "game:paper", *extra)
        return q, q_fb

    q_c,   q_c_fb = _pair("common")
    q_u,   q_u_fb = _pair("uncommon")
    q_dfc, q_dfc_fb = _pair("uncommon", "is:dfc")
    q_r,   q_r_fb = _pair("rare")
    q_m,   q_m_fb = _pair("mythic")

    q_bl = _q(f"set:{sc}", "is:borderless", "(rarity:rare or rarity:mythic)",
              "is:booster", "game:paper", "finish:foil")
    q_bl_fb = _q(f"set:{sc}", "is:borderless",
                 "(rarity:rare or rarity:mythic)", "is:booster", "game:paper")
    q_retro = _q(f"set:{sc}", "frame:1997", "is:booster",
                 "game:paper", "finish:foil")
    q_retro_fb = _q(f"set:{sc}", "frame:1997", "is:booster", "game:paper")
    q_cmdr = _q("set:m3c", "rarity:mythic", "game:paper", "finish:foil")
    q_cmdr_fb = _q("set:m3c", "rarity:mythic", "game:paper")
    q_snow = _q(f"set:{sc}", 'name:"Snow-Covered Wastes"',
                "game:paper", "finish:foil")
    q_snow_fb = _q(f"set:{sc}", 'name:"Snow-Covered Wastes"', "game:paper")

    return Slot(
        name="Traditional foil (wildcard breakdown; finish:foil; unique=cards)",
        outcomes=[
            (cfg.wc_p_c,             QueryPool("mh3_foil_c",            q_c,
             fallback=q_c_fb,    unique="cards", price_field="usd_foil")),
            (cfg.wc_p_u,             QueryPool("mh3_foil_u",            q_u,
             fallback=q_u_fb,    unique="cards", price_field="usd_foil")),
            (cfg.wc_p_dfc_u,         QueryPool("mh3_foil_dfc_u",        q_dfc,
             fallback=q_dfc_fb,  unique="cards", price_field="usd_foil")),
            (cfg.wc_p_r,             QueryPool("mh3_foil_r",            q_r,
             fallback=q_r_fb,    unique="cards", price_field="usd_foil")),
            (cfg.wc_p_m,             QueryPool("mh3_foil_m",            q_m,
             fallback=q_m_fb,    unique="cards", price_field="usd_foil")),
            (cfg.wc_p_borderless_rm, QueryPool("mh3_foil_borderless",   q_bl,
             fallback=q_bl_fb,   unique="cards", price_field="usd_foil")),
            (cfg.wc_p_retro,         QueryPool("mh3_foil_retro",        q_retro,
             fallback=q_retro_fb, unique="cards", price_field="usd_foil")),
            (cfg.wc_p_cmdr_mythic,   QueryPool("mh3_foil_cmdr_mythic",  q_cmdr,
             fallback=q_cmdr_fb,  unique="cards", price_field="usd_foil")),
            (cfg.wc_p_snow_wastes,   QueryPool("mh3_foil_snow_wastes",  q_snow,
             fallback=q_snow_fb,  unique="cards", price_field="usd_foil")),
        ],
        strict_probs=True,
        renormalize=True,
    )


def slot_mh3_land_or_common(cfg: MH3Config = MH3) -> Slot:
    sc = cfg.set_code
    q_common = _q(f"set:{sc}", "rarity:common", "is:booster", "game:paper")
    q_common_fb = _q(f"set:{sc}", "rarity:common", "game:paper")
    q_basic = _q(f"set:{sc}", "type:basic", "-is:fullart",
                 "is:booster", "game:paper")
    q_basic_fb = _q(f"set:{sc}", "type:basic", "-is:fullart", "game:paper")
    q_eldrazi = _q(f"set:{sc}", "type:basic",
                   "is:fullart", "is:booster", "game:paper")
    q_eldrazi_fb = _q(f"set:{sc}", "type:basic",  "is:fullart", "game:paper")

    return Slot(
        name="Land card or common",
        outcomes=[
            (cfg.lc_p_common,           QueryPool("mh3_lc_common",      q_common,
             fallback=q_common_fb,  unique="prints", price_field="usd")),
            (cfg.lc_p_basic_nf,         QueryPool("mh3_basic_nf",       q_basic,
             fallback=q_basic_fb,   unique="prints", price_field="usd")),
            (cfg.lc_p_basic_f,          QueryPool("mh3_basic_f",        q_basic,
             fallback=q_basic_fb,   unique="prints", price_field="usd_foil")),
            (cfg.lc_p_eldrazi_basic_nf, QueryPool("mh3_eldrazi_nf",     q_eldrazi,
             fallback=q_eldrazi_fb, unique="prints", price_field="usd")),
            (cfg.lc_p_eldrazi_basic_f,  QueryPool("mh3_eldrazi_f",      q_eldrazi,
             fallback=q_eldrazi_fb, unique="prints", price_field="usd_foil")),
        ],
        strict_probs=True,
        renormalize=True,
    )


def slot_mh3_special_guests(cfg: MH3Config = MH3) -> Slot:
    q_spg = _q("set:spg", "cn>=39", "cn<=48", "game:paper")
    return Slot(
        name="Special Guests replacement (1 in 64)",
        outcomes=[
            (1.0 - cfg.special_guest_rate, 0.0),
            (cfg.special_guest_rate, QueryPool("mh3_spg", q_spg,
             fallback=q_spg, unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_mh3_play_box() -> ProductModel:
    return ProductModel(
        set_code=MH3.set_code,
        packs_per_box=MH3.packs_per_box,
        slots=[
            slot_mh3_main_rm(),
            slot_mh3_new_to_modern(),
            slot_mh3_wildcard(),
            slot_mh3_traditional_foil(),
            slot_mh3_land_or_common(),
            slot_mh3_special_guests(),
        ],
    )


# ----------------------------
# Printing in the format you want
# ----------------------------
def print_report(r: EVReport) -> None:
    print(f"{r.set_name} ({r.set_code})")
    print(f"Pack EV: ${r.pack_ev:,.2f}")
    print(f"Box EV:  ${r.box_ev:,.2f}   (packs: {r.packs_per_box})")

    print("\nCounts:")
    for k in sorted(r.counts):
        print(f"  {k:<22} {r.counts[k]}")

    print("\nSlot breakdown:")
    for se in r.slot_evals:
        print(f"  {se.name:<34} ${se.ev:,.4f}")
        for pe in se.pool_evals:
            print(f"    - {pe.label:<18} n={pe.count:<4} ev=${pe.ev:,.4f}")

    print("\nWarnings:")
    if not r.warnings:
        print("  (none)")
    else:
        for w in r.warnings:
            print(f"  - {w}")


# ---------------------------------------------------------------------------
# Registry: maps (SET_CODE, kind) -> factory function
# To add a new set: write a model_xxx() function above, then add one line here.
# No other file needs to change for EV support.
# ---------------------------------------------------------------------------
MODEL_REGISTRY: dict[tuple[str, str], Callable[[], "ProductModel"]] = {
    ("OTJ", "box"):       model_otj_play_box,
    ("WOE", "box"):       model_woe_set_box,
    ("WOE", "draft_box"): model_woe_draft_box,
    ("MH3", "box"):       model_mh3_play_box,
    ("ECL", "box"):       model_ecl_play_box,
    ("TLA", "box"):       model_tla_play_box,
}


def model_for_code(code: str, kind: str = "box") -> "ProductModel | None":
    """Return a fresh ProductModel for the given set-code + product kind, or None."""
    key = (code.strip().upper(), kind.strip().lower())
    factory = MODEL_REGISTRY.get(key)
    return factory() if factory else None


def main():
    supported = ", ".join(f"{c}/{k}" for c, k in sorted(MODEL_REGISTRY))
    user_input = input(f"Enter set code/kind [{supported}]: ").strip().upper()
    parts = user_input.split("/")
    set_code = parts[0]
    kind = parts[1].lower() if len(parts) > 1 else "box"
    m = model_for_code(set_code, kind)
    if not m:
        print(f"Unsupported set/kind: {set_code}/{kind}")
        return
    r = m.run()
    print_report(r)


if __name__ == "__main__":
    main()
