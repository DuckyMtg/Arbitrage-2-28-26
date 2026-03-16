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
    "Accept":     "application/json",
}

_RARITY_COUNTS_TTL: int = 7 * 24 * 3600
_rarity_counts_cache: dict[str, tuple[dict[str, int], float]] = {}

# ----------------------------
# Robust HTTP
# ----------------------------


def scryfall_get(
    url: str,
    *,
    params: dict | None = None,
    timeout: int = 30,
    max_retries: int = 6,
    backoff: float = 0.5,
) -> requests.Response:
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
        return cached
    full_cards = _fetch_all_cards_uncached(query, unique=unique)
    minimal = [{"prices": c.get("prices") or {}} for c in full_cards]
    ev_cache.cache_set_json(cache_key, minimal, ev_cache.TTL_CARDS)
    return minimal


def avg_price_usd(
    query: str,
    *,
    price_field: str = "usd",
    unique: str = "prints",
    warnings: list[str] | None = None,
) -> float:
    from app.services import ev_cache
    cache_key = ev_cache.key_avg(query, unique, price_field)
    cached = ev_cache.cache_get_json(cache_key)
    if isinstance(cached, (int, float)):
        return float(cached)
    cards = fetch_all_cards(query, unique=unique)
    if not cards:
        ev_cache.cache_set_json(cache_key, 0.0, ev_cache.TTL_AVG)
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
            pass
    if n_priced == 0:
        ev_cache.cache_set_json(cache_key, 0.0, ev_cache.TTL_AVG)
        return 0.0
    if warnings is not None and n_priced < n_total * 0.8:
        pct = int(100 * n_priced / n_total)
        warnings.append(
            f"avg_price_usd: only {n_priced}/{n_total} cards ({pct}%) "
            f"have a '{price_field}' price for query: {query!r}"
        )
    result = total / n_priced
    ev_cache.cache_set_json(cache_key, result, ev_cache.TTL_AVG)
    return result


# ----------------------------
# Reporting
# ----------------------------

@dataclass
class PoolEval:
    label:     str
    used_query: str
    count:     int
    ev:        float


@dataclass
class SlotEval:
    name:       str
    ev:         float
    pool_evals: list[PoolEval] = field(default_factory=list)


@dataclass
class EVReport:
    set_code:     str
    set_name:     str
    packs_per_box: int
    pack_ev:      float
    box_ev:       float
    slot_evals:   list[SlotEval]
    warnings:     list[str]
    counts:       dict[str, int]   # pool-label -> count


# ----------------------------
# Pool primitives
# ----------------------------

@dataclass(frozen=True)
class QueryPool:
    """
    Prices cards returned by a Scryfall query.
    If primary returns 0 cards, optional fallback is used.
    """
    label:       str
    primary:     str
    fallback:    Optional[str] = None
    unique:      str = "prints"
    price_field: str = "usd"

    def eval(self, warnings: list[str]) -> PoolEval:
        cards_primary = fetch_all_cards(self.primary, unique=self.unique)
        if cards_primary:
            ev = avg_price_usd(self.primary, price_field=self.price_field,
                               unique=self.unique, warnings=warnings)
            return PoolEval(self.label, self.primary, len(cards_primary), ev)
        if self.fallback:
            cards_fb = fetch_all_cards(self.fallback, unique=self.unique)
            if cards_fb:
                warnings.append(
                    f"[{self.label}] primary query returned 0 cards; used fallback.")
                ev = avg_price_usd(self.fallback, price_field=self.price_field,
                                   unique=self.unique, warnings=warnings)
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
    name:        str
    outcomes:    list[tuple[float, OutcomeValue]]
    strict_probs: bool = True
    tol:         float = 1e-6
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
        ev: float = 0.0
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
    set_code:     str
    packs_per_box: int
    slots:        list[Slot]

    def run(self) -> EVReport:
        warnings: list[str] = []
        counts:   dict[str, int] = {}
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
    key = set_code.strip().upper()
    entry = _rarity_counts_cache.get(key)
    if entry is not None:
        data, expires_at = entry
        if time.time() < expires_at:
            return data
    counts: dict[str, int] = {}
    for r in ["common", "uncommon", "rare", "mythic"]:
        q = _q(f"set:{key}", f"rarity:{r}", "is:booster", "game:paper")
        counts[r] = len(fetch_all_cards(q, unique="cards"))
    _rarity_counts_cache[key] = (counts, time.time() + _RARITY_COUNTS_TTL)
    return counts


def _normalize_exact_name(n: str) -> str:
    return n.replace("\u2019", "'").strip()


def name_or_clause(names: list[str]) -> str:
    quoted = [f'"{_normalize_exact_name(n)}"' for n in names]
    return " or ".join(quoted)


def slot_any_rarity_from_set(
    *,
    slot_name:        str,
    pool_label_prefix: str,
    set_code:         str,
    price_field:      str,
    unique:           str = "prints",
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
            outcomes=[(1.0, QueryPool(f"{pool_label_prefix}_any", q,
                                      unique=unique, price_field=price_field))],
            strict_probs=True,
        )
    outcomes: list[tuple[float, OutcomeValue]] = []
    for r in ("common", "uncommon", "rare", "mythic"):
        n = c.get(r, 0)
        if n <= 0:
            continue
        p = n / total
        q = _q(f"set:{set_code}", f"rarity:{r}", "is:booster", "game:paper")
        outcomes.append((p, QueryPool(f"{pool_label_prefix}_{r}", q,
                                      unique=unique, price_field=price_field)))
    return Slot(name=slot_name, outcomes=outcomes, strict_probs=True)


# ============================================================
# Config / Builder pattern
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
    label:             str
    query_filters:     list[str]
    rate:              float
    foil_rate:         float = 0.0
    unique:            str = "prints"
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
    set_code:      str
    packs_per_box: int
    # --- Main R/M slot ---
    mythic_rate:         float = DEFAULT_MYTHIC_RATE
    borderless_fraction: float = 0.0
    # --- Wildcard slot ---
    wc_rates:          RarityRates | None = None
    wc_rm_rate:        float | None = None
    wc_slots_per_pack: int = 1
    # --- Foil slot ---
    foil_rates:  RarityRates | None = None
    # --- Land slot ---
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
        p_c = (c.get("common",   0) / cu) if cu else 0.5
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
    # When s > 1 (e.g. WOE set box has 2 wildcard slots per pack), renormalize=True
    # would cancel the s multiplier by dividing each weight by total_p = s × sum_rates.
    # strict_probs=False with renormalize=False preserves the s factor so the slot
    # correctly contributes s × per-slot-EV to the pack total.
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
        # only enforce prob-sum=1 for single-slot wildcards
        strict_probs=(s == 1),
        renormalize=False,       # never renormalize: s multiplier must be preserved
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
        return QueryPool(f"{sc}_foil_{label}", q, fallback=q_fb, unique="cards", price_field="usd_foil")

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
    """Land slot built from a list of LandTypeConfig entries."""
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
    Pass extra_slots for anything set-specific.
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
    set_code:      str
    packs_per_box: int
    main_p_r:   float
    main_p_m:  float
    main_p_tr: float
    main_p_tm: float
    reg_rare_cn_max: int
    reg_mythic_cn_max: int
    treat_rare_cn_min: int
    treat_mythic_cn_min: int
    wc_p_c: float
    wc_p_u: float
    wc_p_su: float
    wc_p_r: float
    wc_p_m: float
    wc_p_tr: float
    wc_p_tm: float
    reg_uncommon_cn_max: int
    special_u_cn_min: int
    foil_p_c: float
    foil_p_u: float
    foil_p_su: float
    foil_p_r: float
    foil_p_m: float
    foil_p_tr: float
    foil_p_tm: float
    land_foil_rate: float = 0.20
    bonus_rate:      float = 0.0
    bonus_set:       str = ""
    bonus_cn_min:    int = 0
    bonus_cn_max:    int = 0
    bonus_label:     str = ""
    bonus_slot_name: str = ""


ECL_CONFIG = TreatmentPlayConfig(
    set_code="ecl", packs_per_box=36,
    main_p_r=70*(459/38000), main_p_m=24*(459/76000),
    main_p_tr=41*(41/23500), main_p_tm=24*(41/47000),
    reg_rare_cn_max=268, reg_mythic_cn_max=253,
    treat_rare_cn_min=297, treat_mythic_cn_min=284,
    wc_p_c=81*(1/450), wc_p_u=100*(29/5000), wc_p_su=10*(9/7700),
    wc_p_r=70*(19/6500), wc_p_m=24*(1/1100), wc_p_tr=41*(3/7700), wc_p_tm=24*(3/15400),
    reg_uncommon_cn_max=263, special_u_cn_min=331,
    foil_p_c=81*(151/20250), foil_p_u=100*(149/50000), foil_p_su=10*(3/3500),
    foil_p_r=70*(1/1000), foil_p_m=24*(1/2000), foil_p_tr=41*(1/3500), foil_p_tm=24*(1/7000),
    land_foil_rate=1/5,
    bonus_rate=5/275, bonus_set="spg", bonus_cn_min=129, bonus_cn_max=148,
    bonus_label="ecl_spg", bonus_slot_name="Special Guests (SPG 129-148, replaces common)",
)

TLA_CONFIG = TreatmentPlayConfig(
    set_code="tla", packs_per_box=36,
    main_p_r=62*(463/35000), main_p_m=26*(463/70000),
    main_p_tr=40*(37/23500), main_p_tm=28*(37/47000),
    reg_rare_cn_max=278, reg_mythic_cn_max=262,
    treat_rare_cn_min=302, treat_mythic_cn_min=297,
    wc_p_c=81*(7/13500), wc_p_u=110*(741/110000), wc_p_su=4*(9/7375),
    wc_p_r=62*(193/70000), wc_p_m=26*(193/140000), wc_p_tr=40*(3/7375), wc_p_tm=28*(3/14750),
    reg_uncommon_cn_max=281, special_u_cn_min=299,
    foil_p_c=81*(539/80757), foil_p_u=110*(367/109670), foil_p_su=4*(36/58823),
    foil_p_r=62*(79/69790), foil_p_m=26*(79/139580), foil_p_tr=40*(12/58823), foil_p_tm=28*(6/58823),
    land_foil_rate=1/5,
    bonus_rate=5/130, bonus_set="tle", bonus_cn_min=1, bonus_cn_max=61,
    bonus_label="tla_source_material", bonus_slot_name="Source Material (TLE, replaces common)",
)


def build_treatment_main_rm_slot(cfg: TreatmentPlayConfig) -> Slot:
    sc = cfg.set_code
    q_r = _q(f"set:{sc}", "rarity:rare",
             f"cn<={cfg.reg_rare_cn_max}",   "is:booster", "game:paper")
    q_m = _q(f"set:{sc}", "rarity:mythic",
             f"cn<={cfg.reg_mythic_cn_max}", "is:booster", "game:paper")
    q_tr = _q(f"set:{sc}", "rarity:rare",
              f"cn>={cfg.treat_rare_cn_min}",   "game:paper")
    q_tm = _q(f"set:{sc}", "rarity:mythic",
              f"cn>={cfg.treat_mythic_cn_min}", "game:paper")
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
        strict_probs=True, renormalize=True,
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
        strict_probs=True, renormalize=True,
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
        return QueryPool(f"{sc}_foil_{label}", q, fallback=q_fb, unique="cards", price_field="usd_foil")

    return Slot(
        name="Traditional foil",
        outcomes=[
            (cfg.foil_p_c,  _fp("c",  "common")),
            (cfg.foil_p_u,  _fp("u",  "uncommon",
             f"cn<={cfg.reg_uncommon_cn_max}", booster=False)),
            (cfg.foil_p_su, _fp("su", "uncommon",
             f"cn>={cfg.special_u_cn_min}",   booster=False)),
            (cfg.foil_p_r,  _fp("r",  "rare",   f"cn<={cfg.reg_rare_cn_max}")),
            (cfg.foil_p_m,  _fp("m",  "mythic",
             f"cn<={cfg.reg_mythic_cn_max}")),
            (cfg.foil_p_tr, _fp("tr", "rare",
             f"cn>={cfg.treat_rare_cn_min}",   booster=False)),
            (cfg.foil_p_tm, _fp("tm", "mythic",
             f"cn>={cfg.treat_mythic_cn_min}", booster=False)),
        ],
        strict_probs=True, renormalize=True,
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
            (cfg.land_foil_rate,        QueryPool(
                f"{sc}_land_f",  q, fallback=q_fb, unique="prints", price_field="usd_foil")),
        ],
        strict_probs=True,
    )


def build_treatment_bonus_slot(cfg: TreatmentPlayConfig) -> Slot:
    q = _q(f"set:{cfg.bonus_set}", f"cn>={cfg.bonus_cn_min}",
           f"cn<={cfg.bonus_cn_max}", "game:paper")
    return Slot(
        name=cfg.bonus_slot_name,
        outcomes=[
            (1.0 - cfg.bonus_rate, 0.0),
            (cfg.bonus_rate, QueryPool(cfg.bonus_label, q,
             fallback=q, unique="prints", price_field="usd")),
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
# WOE Draft Booster
# ============================================================

_WOT_DRAFT_P_U = 18 * (4 / 147) + 25 * (2 / 147)
_WOT_DRAFT_P_R = 5 * (1 / 98) + 15 * (1 / 196)
_WOT_DRAFT_P_AR = 5 * (1 / 294)
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
    p_foil = 1 / 3
    _p_fu = 0.2500
    _p_fr = 0.1210
    _p_fm = 0.0297

    def _fp(label: str, rarity: str) -> QueryPool:
        q = _q("(set:woe or set:wot)",
               f"rarity:{rarity}", "finish:foil", "game:paper")
        q_fb = _q("(set:woe or set:wot)", f"rarity:{rarity}", "game:paper")
        return QueryPool(f"woe_draft_foil_{label}", q, fallback=q_fb, unique="cards", price_field="usd_foil")

    return Slot(
        name="Foil (replaces common, 1/3 packs)",
        outcomes=[
            (1 - p_foil * (_p_fu + _p_fr + _p_fm), 0.0),
            (p_foil * _p_fu, _fp("u", "uncommon")),
            (p_foil * _p_fr, _fp("r", "rare")),
            (p_foil * _p_fm, _fp("m", "mythic")),
        ],
        strict_probs=True, renormalize=True,
    )


WOE_DRAFT_CONFIG = PlayBoosterConfig(
    set_code="woe", packs_per_box=36,
    mythic_rate=20 / 138,
    wc_rates=RarityRates(), wc_slots_per_pack=0,
)


def model_woe_draft_box() -> ProductModel:
    return ProductModel(
        set_code=WOE_DRAFT_CONFIG.set_code,
        packs_per_box=WOE_DRAFT_CONFIG.packs_per_box,
        slots=[
            build_main_rm_slot(WOE_DRAFT_CONFIG),
            slot_woe_draft_enchanting_tales(),
            slot_woe_draft_foil(),
        ],
    )


# ============================================================
# OTJ
# ============================================================

OTJ_CONFIG = PlayBoosterConfig(
    set_code="otj", packs_per_box=36,
    mythic_rate=DEFAULT_MYTHIC_RATE,
    borderless_fraction=0.40,
    wc_rm_rate=1 / 12,
    land_types=[
        LandTypeConfig("dual",  ["type:land", "rarity:common",
                       "-type:basic"], rate=1/2, foil_rate=1/5),
        LandTypeConfig("west",  ["type:basic", "is:fullart"],
                       rate=1/6, foil_rate=1/5),
        LandTypeConfig("basic", ["type:basic", "-is:fullart"],
                       rate=1/3, foil_rate=1/5),
    ],
)


def slot_otj_breaking_news() -> Slot:
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
            (p_big, QueryPool("otj_list_big", q_big,
             fallback=q_big, unique="cards",  price_field="usd")),
            (p_spg, QueryPool("otj_list_spg", q_spg, fallback=_q(
                "set:spg", "game:paper"), unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_otj_play_box() -> ProductModel:
    return model_from_config(OTJ_CONFIG, extra_slots=[slot_otj_breaking_news(), slot_otj_the_list()])


# ============================================================
# WOE (Set Booster / Play Booster)
# ============================================================

WOE_CONFIG = PlayBoosterConfig(
    set_code="woe", packs_per_box=30,
    mythic_rate=20 / 138,
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

_WOT_ANIME_RARE_RATE = 5 / 249
_WOT_ANIME_MYTHIC_RATE = 15 / 588
_WOT_RARITY_WEIGHTS = {"uncommon": 72/147, "rare": 60/147, "mythic": 15/147}
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
            (p_list, QueryPool("woe_list_plst", q_plst, fallback=_q("set:plst", "game:paper"),
                               unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_woe_set_box() -> ProductModel:
    return model_from_config(WOE_CONFIG, extra_slots=[slot_woe_enchanting_tales(), slot_woe_the_list()])


# ============================================================
# MH3
# ============================================================

@dataclass
class MH3Config:
    set_code:      str = "mh3"
    packs_per_box: int = 36
    main_p_r:               float = 0.798
    main_p_m:               float = 0.130
    main_p_retro_total:     float = 0.021
    main_p_borderless_total: float = 0.051
    main_retro_r_count:     int = 24
    main_retro_m_count:     int = 8
    ntm_p_u:                     float = 0.750
    ntm_p_r:                     float = 0.213
    ntm_p_m:                     float = 0.023
    ntm_p_framebreak_total:      float = 0.008
    ntm_p_profile_total:         float = 0.003
    ntm_p_retro_total:           float = 0.002
    ntm_p_extra_borderless_mythic: float = 0.0005
    wc_p_c:             float = 0.417
    wc_p_u:             float = 0.334
    wc_p_dfc_u:         float = 0.083
    wc_p_r:             float = 0.067
    wc_p_m:             float = 0.011
    wc_p_borderless_rm: float = 0.004
    wc_p_retro:         float = 0.042
    wc_p_cmdr_mythic:   float = 0.042
    wc_p_snow_wastes:   float = 0.0005
    lc_p_common:           float = 0.50
    lc_p_basic_nf:         float = 0.20
    lc_p_basic_f:          float = 0.133
    lc_p_eldrazi_basic_nf: float = 0.10
    lc_p_eldrazi_basic_f:  float = 0.067
    special_guest_rate:    float = 1 / 64


MH3 = MH3Config()


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
    q_bl = _q(f"set:{sc}", "is:borderless",
              "(rarity:rare or rarity:mythic)", "game:paper")
    return Slot(
        name="Main R/M (incl. retro + borderless)",
        outcomes=[
            (cfg.main_p_r,               QueryPool("mh3_main_regular_r",  q_r_reg,
             fallback=q_r_reg_fb,  unique="prints", price_field="usd")),
            (cfg.main_p_m,               QueryPool("mh3_main_regular_m",  q_m_reg,
             fallback=q_m_reg_fb,  unique="prints", price_field="usd")),
            (p_retro_r,                  QueryPool("mh3_main_retro_r",    q_r_retro,
             fallback=q_r_retro_fb, unique="prints", price_field="usd")),
            (p_retro_m,                  QueryPool("mh3_main_retro_m",    q_m_retro,
             fallback=q_m_retro_fb, unique="prints", price_field="usd")),
            (cfg.main_p_borderless_total, QueryPool("mh3_main_borderless", q_bl,
             fallback=q_bl,         unique="prints", price_field="usd")),
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
            (cfg.ntm_p_u, QueryPool("mh3_ntm_u_reg",         q_u_reg,
             fallback=q_u_reg_fb,     unique="prints", price_field="usd")),
            (cfg.ntm_p_r, QueryPool("mh3_ntm_r_reg",         q_r_reg,
             fallback=q_r_reg_fb,     unique="prints", price_field="usd")),
            (cfg.ntm_p_m, QueryPool("mh3_ntm_m_reg",         q_m_reg,
             fallback=q_m_reg_fb,     unique="prints", price_field="usd")),
            (p_retro_r_m, QueryPool("mh3_ntm_retro_any",     q_retro_any,
             fallback=q_retro_any_fb, unique="prints", price_field="usd")),
            (p_bl_r,      QueryPool("mh3_ntm_borderless_any", q_bl_any,
             fallback=q_bl_any,       unique="prints", price_field="usd")),
            (p_bl_m,      QueryPool("mh3_ntm_borderless_m",   q_bl_m,
             fallback=q_bl_m,          unique="prints", price_field="usd")),
        ],
        strict_probs=True, renormalize=True,
    )


def slot_mh3_wildcard(cfg: MH3Config = MH3) -> Slot:
    sc = cfg.set_code

    def _q_pair(rarity: str, *extra: str):
        return (
            _q(f"set:{sc}", f"rarity:{rarity}",
               "is:booster", "game:paper", *extra),
            _q(f"set:{sc}", f"rarity:{rarity}", "game:paper", *extra),
        )
    q_c,   q_c_fb = _q_pair("common")
    q_u,   q_u_fb = _q_pair("uncommon")
    q_dfc, q_dfc_fb = _q_pair("uncommon", "is:dfc")
    q_r,   q_r_fb = _q_pair("rare")
    q_m,   q_m_fb = _q_pair("mythic")
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
        strict_probs=True, renormalize=True,
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
            (cfg.wc_p_c,             QueryPool("mh3_foil_c",           q_c,
             fallback=q_c_fb,    unique="cards", price_field="usd_foil")),
            (cfg.wc_p_u,             QueryPool("mh3_foil_u",           q_u,
             fallback=q_u_fb,    unique="cards", price_field="usd_foil")),
            (cfg.wc_p_dfc_u,         QueryPool("mh3_foil_dfc_u",       q_dfc,
             fallback=q_dfc_fb,  unique="cards", price_field="usd_foil")),
            (cfg.wc_p_r,             QueryPool("mh3_foil_r",           q_r,
             fallback=q_r_fb,    unique="cards", price_field="usd_foil")),
            (cfg.wc_p_m,             QueryPool("mh3_foil_m",           q_m,
             fallback=q_m_fb,    unique="cards", price_field="usd_foil")),
            (cfg.wc_p_borderless_rm, QueryPool("mh3_foil_borderless",  q_bl,
             fallback=q_bl_fb,   unique="cards", price_field="usd_foil")),
            (cfg.wc_p_retro,         QueryPool("mh3_foil_retro",       q_retro,
             fallback=q_retro_fb, unique="cards", price_field="usd_foil")),
            (cfg.wc_p_cmdr_mythic,   QueryPool("mh3_foil_cmdr_mythic", q_cmdr,
             fallback=q_cmdr_fb,  unique="cards", price_field="usd_foil")),
            (cfg.wc_p_snow_wastes,   QueryPool("mh3_foil_snow_wastes", q_snow,
             fallback=q_snow_fb,  unique="cards", price_field="usd_foil")),
        ],
        strict_probs=True, renormalize=True,
    )


def slot_mh3_land_or_common(cfg: MH3Config = MH3) -> Slot:
    sc = cfg.set_code
    q_common = _q(f"set:{sc}", "rarity:common", "is:booster", "game:paper")
    q_common_fb = _q(f"set:{sc}", "rarity:common", "game:paper")
    q_basic = _q(f"set:{sc}", "type:basic", "-is:fullart",
                 "is:booster", "game:paper")
    q_basic_fb = _q(f"set:{sc}", "type:basic", "-is:fullart", "game:paper")
    q_eldrazi = _q(f"set:{sc}", "type:basic",
                   "is:fullart",  "is:booster", "game:paper")
    q_eldrazi_fb = _q(f"set:{sc}", "type:basic", "is:fullart", "game:paper")
    return Slot(
        name="Land card or common",
        outcomes=[
            (cfg.lc_p_common,           QueryPool("mh3_lc_common",  q_common,
             fallback=q_common_fb,   unique="prints", price_field="usd")),
            (cfg.lc_p_basic_nf,         QueryPool("mh3_basic_nf",   q_basic,
             fallback=q_basic_fb,    unique="prints", price_field="usd")),
            (cfg.lc_p_basic_f,          QueryPool("mh3_basic_f",    q_basic,
             fallback=q_basic_fb,    unique="prints", price_field="usd_foil")),
            (cfg.lc_p_eldrazi_basic_nf, QueryPool("mh3_eldrazi_nf", q_eldrazi,
             fallback=q_eldrazi_fb,  unique="prints", price_field="usd")),
            (cfg.lc_p_eldrazi_basic_f,  QueryPool("mh3_eldrazi_f",  q_eldrazi,
             fallback=q_eldrazi_fb,  unique="prints", price_field="usd_foil")),
        ],
        strict_probs=True, renormalize=True,
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
        set_code=MH3.set_code, packs_per_box=MH3.packs_per_box,
        slots=[
            slot_mh3_main_rm(), slot_mh3_new_to_modern(), slot_mh3_wildcard(),
            slot_mh3_traditional_foil(), slot_mh3_land_or_common(), slot_mh3_special_guests(),
        ],
    )


# ============================================================
# Shared helpers for newer sets
# ============================================================

def slot_replaces_common_with_pool(
    *, slot_name: str, replace_rate: float, pool_label: str, query: str
) -> Slot:
    """Generic 'replaces a common' bonus slot."""
    return Slot(
        name=slot_name,
        outcomes=[
            (1.0 - replace_rate, 0.0),
            (replace_rate, QueryPool(pool_label, query,
             fallback=query, unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def _std_land_types_any_land(*, foil_rate: float = 0.20) -> list[LandTypeConfig]:
    return [LandTypeConfig("any", ["type:land"], rate=1.0, foil_rate=foil_rate)]


def _std_land_types_basic_only(*, foil_rate: float = 0.20) -> list[LandTypeConfig]:
    return [LandTypeConfig("basic", ["type:basic"], rate=1.0, foil_rate=foil_rate)]


# ============================================================
# BLB — Bloomburrow (36 packs/box)
# SPG 54–63, replaces a common in 15/1000 packs
# ============================================================

BLB_CONFIG = PlayBoosterConfig(
    set_code="blb", packs_per_box=36,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_any_land(foil_rate=0.20),
)


def slot_blb_special_guests() -> Slot:
    return slot_replaces_common_with_pool(
        slot_name="Special Guests replacement (15 in 1000)",
        replace_rate=15/1000, pool_label="blb_spg",
        query=_q("set:spg", "cn>=54", "cn<=63", "game:paper"),
    )


def model_blb_play_box() -> ProductModel:
    return model_from_config(BLB_CONFIG, extra_slots=[slot_blb_special_guests()])


# ============================================================
# DSK — Duskmourn: House of Horror (36 packs/box)
# SPG 64–73, replaces a common in 1/64 packs
# ============================================================

DSK_CONFIG = PlayBoosterConfig(
    set_code="dsk", packs_per_box=36,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_basic_only(foil_rate=0.20),
)


def slot_dsk_special_guests() -> Slot:
    return slot_replaces_common_with_pool(
        slot_name="Special Guests replacement (1 in 64)",
        replace_rate=1/64, pool_label="dsk_spg",
        query=_q("set:spg", "cn>=64", "cn<=73", "game:paper"),
    )


def model_dsk_play_box() -> ProductModel:
    return model_from_config(DSK_CONFIG, extra_slots=[slot_dsk_special_guests()])


# ============================================================
# DFT — Aetherdrift (30 packs/box)
# SPG 84–93, replaces a common in 1/64 packs
# ============================================================

DFT_CONFIG = PlayBoosterConfig(
    set_code="dft", packs_per_box=30,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_any_land(foil_rate=0.20),
)


def slot_dft_special_guests() -> Slot:
    return slot_replaces_common_with_pool(
        slot_name="Special Guests replacement (1 in 64)",
        replace_rate=1/64, pool_label="dft_spg",
        query=_q("set:spg", "cn>=84", "cn<=93", "game:paper"),
    )


def model_dft_play_box() -> ProductModel:
    return model_from_config(DFT_CONFIG, extra_slots=[slot_dft_special_guests()])


# ============================================================
# FDN — Foundations (36 packs/box)
# SPG 74–83, replaces a common in 3/200 packs
# ============================================================

FDN_CONFIG = PlayBoosterConfig(
    set_code="fdn", packs_per_box=36,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_any_land(foil_rate=0.20),
)


def slot_fdn_special_guests() -> Slot:
    return slot_replaces_common_with_pool(
        slot_name="Special Guests replacement (3 in 200)",
        replace_rate=3/200, pool_label="fdn_spg",
        query=_q("set:spg", "cn>=74", "cn<=83", "game:paper"),
    )


def model_fdn_play_box() -> ProductModel:
    return model_from_config(FDN_CONFIG, extra_slots=[slot_fdn_special_guests()])


# ============================================================
# FIN — Final Fantasy (30 packs/box)
# Through the Ages (FCA), replaces a common in 1/3 packs
# ============================================================

FIN_CONFIG = PlayBoosterConfig(
    set_code="fin", packs_per_box=30,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_basic_only(foil_rate=0.20),
)


def slot_fin_through_the_ages() -> Slot:
    return slot_replaces_common_with_pool(
        slot_name="Through the Ages replacement (1 in 3)",
        replace_rate=1/3, pool_label="fin_fca",
        query=_q("set:fca", "game:paper"),
    )


def model_fin_play_box() -> ProductModel:
    return model_from_config(FIN_CONFIG, extra_slots=[slot_fin_through_the_ages()])


# ============================================================
# EOE — Edge of Eternities (30 packs/box)
# SPG 119–128, replaces a common in 9/500 packs
# ============================================================

EOE_CONFIG = PlayBoosterConfig(
    set_code="eoe", packs_per_box=30,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_basic_only(foil_rate=0.20),
)


def slot_eoe_special_guests() -> Slot:
    return slot_replaces_common_with_pool(
        slot_name="Special Guests replacement (9 in 500)",
        replace_rate=9/500, pool_label="eoe_spg",
        query=_q("set:spg", "cn>=119", "cn<=128", "game:paper"),
    )


def model_eoe_play_box() -> ProductModel:
    return model_from_config(EOE_CONFIG, extra_slots=[slot_eoe_special_guests()])


# ============================================================
# TDM — Tarkir: Dragonstorm (30 packs/box)
# SPG 104–113, replaces a common in 1/64 packs
# ============================================================

TDM_CONFIG = PlayBoosterConfig(
    set_code="tdm", packs_per_box=30,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_any_land(foil_rate=0.20),
)


def slot_tdm_special_guests() -> Slot:
    return slot_replaces_common_with_pool(
        slot_name="Special Guests replacement (1 in 64)",
        replace_rate=1/64, pool_label="tdm_spg",
        query=_q("set:spg", "cn>=104", "cn<=113", "game:paper"),
    )


def model_tdm_play_box() -> ProductModel:
    return model_from_config(TDM_CONFIG, extra_slots=[slot_tdm_special_guests()])


# ============================================================
# INR — Innistrad Remastered (36 packs/box)
# Retro slot cn 329–480 appears once per pack
# ============================================================

INR_CONFIG = PlayBoosterConfig(
    set_code="inr", packs_per_box=36,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_basic_only(foil_rate=0.20),
)


def slot_inr_retro() -> Slot:
    q_retro = _q("set:inr", "cn>=329", "cn<=480", "game:paper")
    return Slot(
        name="Retro slot",
        outcomes=[(1.0, QueryPool("inr_retro", q_retro,
                   fallback=q_retro, unique="prints", price_field="usd"))],
        strict_probs=True,
    )


def model_inr_play_box() -> ProductModel:
    return model_from_config(INR_CONFIG, extra_slots=[slot_inr_retro()])


# ============================================================
# SPM — Marvel's Spider-Man (30 packs/box)
# Source Material MAR 1–40, replaces a common in 1/24 packs
# ============================================================

SPM_CONFIG = PlayBoosterConfig(
    set_code="spm", packs_per_box=30,
    mythic_rate=DEFAULT_MYTHIC_RATE, wc_rm_rate=1/12,
    land_types=_std_land_types_any_land(foil_rate=0.20),
)


def slot_spm_source_material() -> Slot:
    return slot_replaces_common_with_pool(
        slot_name="Source Material replacement (1 in 24)",
        replace_rate=1/24, pool_label="spm_mar",
        query=_q("set:mar", "cn>=1", "cn<=40", "game:paper"),
    )


def model_spm_play_box() -> ProductModel:
    return model_from_config(SPM_CONFIG, extra_slots=[slot_spm_source_material()])


# ============================================================
# ACR — Magic: The Gathering–Assassin's Creed (24 packs/box)
# Product type: Beyond Booster — unique slot structure.
#
# Sources: mtg.wtf/pack/acr + magic.wizards.com collecting article.
#
# Slots verified from mtg.wtf sheet rates:
#   3 non-foil uncommons per pack (Sheet uncommon, 1/54 per card × 54 cards)
#   1 land/scene slot: 96.6% full-art basic (omitted), 3.09% rare scene dual,
#                      0.31% mythic scene Ezio (cn 111–116)
#   1 non-foil R/M: 17.95% mythic (32 rares at 1/39 + 14 mythics at 1/78)
#   1 traditional foil: 83.34% unc / 13.67% rare / 2.99% mythic
#   1 Booster Fun card: non-foil (83.4%) or foil (16.6%) showcase/borderless
#     — rare/mythic rates from Wizards article; showcase uncommons omitted
# ============================================================

def model_acr_beyond_box() -> ProductModel:
    sc = "acr"
    q_unc = _q(f"set:{sc}", "rarity:uncommon",  "is:booster", "game:paper")
    q_rare = _q(f"set:{sc}", "rarity:rare",       "is:booster", "game:paper")
    q_myth = _q(f"set:{sc}", "rarity:mythic",     "is:booster", "game:paper")
    # Rome Vista scene: 5 rare duals + 1 mythic Ezio, all cn 111–116
    q_scene_r = _q(f"set:{sc}", "cn>=111", "cn<=116",
                   "rarity:rare",   "game:paper")
    q_scene_m = _q(f"set:{sc}", "cn>=111", "cn<=116",
                   "rarity:mythic", "game:paper")
    # Booster Fun treatments
    q_sc_r = _q(f"set:{sc}", "rarity:rare",   "is:showcase",   "game:paper")
    q_sc_m = _q(f"set:{sc}", "rarity:mythic", "is:showcase",   "game:paper")
    q_bl_r = _q(f"set:{sc}", "rarity:rare",   "is:borderless", "game:paper")
    q_bl_m = _q(f"set:{sc}", "rarity:mythic", "is:borderless", "game:paper")
    return ProductModel(
        set_code=sc, packs_per_box=24,
        slots=[
            # 3 non-foil uncommons — probability 3.0 with strict_probs=False
            Slot(
                name="3 uncommons",
                outcomes=[
                    (3.0, QueryPool(f"{sc}_unc3", q_unc, unique="cards", price_field="usd"))],
                strict_probs=False,
            ),
            # Land / scene slot (96.6% basic omitted as 0.0 EV)
            Slot(
                name="Land or scene card (rare 3.09% / mythic 0.31%)",
                outcomes=[
                    (0.0309, QueryPool(
                        f"{sc}_scene_r", q_scene_r, fallback=q_rare, unique="cards", price_field="usd")),
                    (0.0031, QueryPool(
                        f"{sc}_scene_m", q_scene_m, fallback=q_myth, unique="cards", price_field="usd")),
                ],
                strict_probs=False,
            ),
            # Main R/M — 17.95% mythic rate from sheet weights
            Slot(
                name="Main R/M (mythic 17.95%)",
                outcomes=[
                    (1 - 0.1795, QueryPool(f"{sc}_main_r",
                     q_rare, unique="prints", price_field="usd")),
                    (0.1795, QueryPool(f"{sc}_main_m", q_myth,
                     unique="prints", price_field="usd")),
                ],
                strict_probs=True,
            ),
            # Traditional foil — rates from Wizards collecting article
            Slot(
                name="Traditional foil (unc 83.34% / rare 13.67% / mythic 2.99%)",
                outcomes=[
                    (0.8334, QueryPool(f"{sc}_foil_u", q_unc,
                     unique="cards", price_field="usd_foil")),
                    (0.1367, QueryPool(f"{sc}_foil_r", q_rare,
                     unique="cards", price_field="usd_foil")),
                    (0.0299, QueryPool(f"{sc}_foil_m", q_myth,
                     unique="cards", price_field="usd_foil")),
                ],
                strict_probs=True,
            ),
            # Booster Fun rare/mythic (showcase uncommons ~69% omitted)
            Slot(
                name="Booster Fun rare/mythic (showcase + borderless, non-foil + foil)",
                outcomes=[
                    (0.0864, QueryPool(
                        f"{sc}_bf_sc_r",   q_sc_r, fallback=q_rare, unique="prints", price_field="usd")),
                    (0.0154, QueryPool(
                        f"{sc}_bf_sc_m",   q_sc_m, fallback=q_myth, unique="prints", price_field="usd")),
                    (0.0123, QueryPool(
                        f"{sc}_bf_bl_r",   q_bl_r, fallback=q_rare, unique="prints", price_field="usd")),
                    (0.0247, QueryPool(
                        f"{sc}_bf_bl_m",   q_bl_m, fallback=q_myth, unique="prints", price_field="usd")),
                    (0.0173, QueryPool(
                        f"{sc}_bf_sc_r_f", q_sc_r, fallback=q_rare, unique="prints", price_field="usd_foil")),
                    (0.0031, QueryPool(
                        f"{sc}_bf_sc_m_f", q_sc_m, fallback=q_myth, unique="prints", price_field="usd_foil")),
                    (0.0025, QueryPool(
                        f"{sc}_bf_bl_r_f", q_bl_r, fallback=q_rare, unique="prints", price_field="usd_foil")),
                    (0.0049, QueryPool(
                        f"{sc}_bf_bl_m_f", q_bl_m, fallback=q_myth, unique="prints", price_field="usd_foil")),
                ],
                strict_probs=False,
            ),
        ],
    )


# ============================================================
# MKM — Murders at Karlov Manor (36 packs/box) — Play Booster
#
# Sources: mtg.wtf/pack/mkm-play + Wizards collecting article.
#
# Wildcard dual-land guarantee verified from mtg.wtf wildcard sheet:
#   10 regular-frame rare duals at 7/480 per card = 14.58% combined
#   10 borderless rare duals at 1/480 per card    =  2.08% combined
#   Total = 16.67% (~1 in 6 packs), matching Wizards article exactly.
#
# SPG collector numbers verified from mtg.wtf the_list sheet: cn 19–28
# (not cn 11–20 as initially estimated). The_list fires in 10% of packs;
# SPG is ~20% of that pool, giving ~2% per-pack rate. We use 1/64 (~1.56%)
# to match the Wizards-stated rate.
# ============================================================

# MKM_CONFIG has no wc_rm_rate — the wildcard slot is built entirely by
# slot_mkm_wildcard() below to avoid double-counting the dual-land guarantee.
MKM_CONFIG = PlayBoosterConfig(
    set_code="mkm", packs_per_box=36,
    mythic_rate=DEFAULT_MYTHIC_RATE,
    land_types=_std_land_types_any_land(foil_rate=0.20),
)


def slot_mkm_wildcard() -> Slot:
    """
    Unified MKM wildcard slot — dual lands and R/M wildcards are mutually
    exclusive outcomes of the same physical slot.

    Rates verified from mtg.wtf wildcard sheet:
      Regular-frame rare duals: 7/480 × 10 cards = 14.58%
      Borderless rare duals:    1/480 × 10 cards =  2.08%
      Remaining 83.33% → standard wildcard at wc_rm_rate=1/12:
        R/M share of 83.33% = 83.33% × 1/12 = 6.94%
        C/U share of 83.33% = 83.33% × 11/12 = 76.39% (omitted, near-zero EV)

    The old implementation used model_from_config (which adds a full
    build_wildcard_slot at prob-sum=1.0) plus slot_mkm_dual_land_wc as a
    separate extra slot — treating them as additive when they are exclusive.
    That overestimated box EV by ~$18–54 depending on dual land prices.
    """
    q_dual = _q("set:mkm", "rarity:rare", "type:land",
                "-is:borderless", "game:paper")
    q_dual_bl = _q("set:mkm", "rarity:rare", "type:land",
                   "is:borderless",  "game:paper")
    q_r = _q("set:mkm", "rarity:rare",   "is:booster", "game:paper")
    q_m = _q("set:mkm", "rarity:mythic", "is:booster", "game:paper")
    p_dual_total = 0.1458 + 0.0208          # 16.67%
    p_wc_rm = (1 - p_dual_total) / 12  # 83.33% × 1/12 ≈ 6.94%
    p_wc_r = p_wc_rm * (1 - DEFAULT_MYTHIC_RATE)
    p_wc_m = p_wc_rm * DEFAULT_MYTHIC_RATE
    return Slot(
        name="Wildcard (dual 16.67% / R/M 6.94% / C+U remainder omitted)",
        outcomes=[
            (0.1458, QueryPool("mkm_dual_reg", q_dual,
             fallback=q_dual,    unique="cards",  price_field="usd")),
            (0.0208, QueryPool("mkm_dual_bl",  q_dual_bl,
             fallback=q_dual_bl, unique="cards",  price_field="usd")),
            (p_wc_r, QueryPool("mkm_wc_r",     q_r,
             unique="prints", price_field="usd")),
            (p_wc_m, QueryPool("mkm_wc_m",     q_m,
             unique="prints", price_field="usd")),
            # C/U wildcard: ~76% of packs, but avg price ≈ $0 — omitted cleanly
        ],
        strict_probs=False,
    )


def slot_mkm_special_guests() -> Slot:
    """
    SPG cn 19–28 (10 cards) verified from mtg.wtf the_list sheet.
    Rate: ~1.56% per Wizards article (modeled as 1/64).
    """
    return slot_replaces_common_with_pool(
        slot_name="Special Guests (MKM, cn 19-28, ~1.56% per pack)",
        replace_rate=1/64, pool_label="mkm_spg",
        query=_q("set:spg", "cn>=19", "cn<=28", "game:paper"),
    )


def model_mkm_play_box() -> ProductModel:
    """
    Built as a raw ProductModel rather than via model_from_config so that
    the wildcard slot is slot_mkm_wildcard() only — model_from_config would
    call build_wildcard_slot() unconditionally, creating a second independent
    wildcard slot that double-counts the dual-land guarantee.
    """
    return ProductModel(
        set_code="mkm", packs_per_box=36,
        slots=[
            build_main_rm_slot(MKM_CONFIG),
            slot_mkm_wildcard(),
            build_foil_slot(MKM_CONFIG),
            build_land_slot(MKM_CONFIG),
            slot_mkm_special_guests(),
        ],
    )


# ============================================================
# RVR — Ravnica Remastered (36 packs/box) — Draft Booster
#
# Sources: mtg.wtf/pack/rvr-draft + Wizards collecting article.
#
# Pack variants (verified from mtg.wtf):
#   60% (18/30): mana_slot + retro_common_uncommon + rare_mythic_with_showcase
#   30%  (9/30): same + traditional foil
#   6.67%(2/30): mana_slot + retro_RARE_MYTHIC (no showcase rare slot)
#   3.33%(1/30): same + traditional foil
#
# Key facts derived from sheet analysis:
#   Mana slot: exactly 58% guildgate / 33% signet / 9% shock+Lantern (verified)
#   Main rare slot fires in 90% of packs; mythic rate = 19.6% (not 12.5%)
#   Retro rare slot fires in 10% of packs; cn 302–407, mythic rate = 11.3%
#   Foil fires in 33.3% of packs — foil_rates scaled by 0.333 to correct
#   No basic lands (suppressed via land_types=[])
#   Retro cards queried by cn range (more reliable than frame:1997)
# ============================================================

# RVR foil rates: card-count-proportional weights scaled by 0.333 foil frequency
# (292 total booster cards: 111 common, 90 uncommon, 71 rare, 20 mythic)
RVR_CONFIG = PlayBoosterConfig(
    set_code="rvr", packs_per_box=36,
    mythic_rate=DEFAULT_MYTHIC_RATE,  # placeholder only; main R/M built manually below
    wc_rm_rate=1/12,
    land_types=[],  # no basic lands in RVR; mana slot handled separately
    foil_rates=RarityRates(
        common=(111/292) * 0.333,
        uncommon=(90/292) * 0.333,
        rare=(71/292) * 0.333,
        mythic=(20/292) * 0.333,
    ),
)


def slot_rvr_mana_slot() -> Slot:
    """
    Replaces the basic land slot in every RVR pack.
    Rates verified from mtg.wtf mana_slot sheet:
      9/1100 per card × 11 cards (10 shocks + Chromatic Lantern) = 9%
      33/1000 per card × 10 signets                              = 33%
      29/500  per card × 10 guildgates                           = 58%
    """
    q_shock = _q("set:rvr", "rarity:rare",    "type:land", "game:paper")
    q_signet = _q("set:rvr", "rarity:uncommon", "signet",   "game:paper")
    return Slot(
        name="Mana slot (shock/lantern 9% / signet 33% / guildgate 58%)",
        outcomes=[
            (0.09, QueryPool("rvr_mana_shock",  q_shock,
             fallback=q_shock,  unique="cards", price_field="usd")),
            (0.33, QueryPool("rvr_mana_signet", q_signet,
             fallback=q_signet, unique="cards", price_field="usd")),
            (0.58, 0.0),
        ],
        strict_probs=True,
    )


def slot_rvr_main_rare() -> Slot:
    """
    Showcase rare/mythic slot — fires in 90% of packs (variants 1 and 2).
    Mythic rate = 19.6%, verified from mtg.wtf rare_mythic_with_showcase sheet
    rate groups (rare groups sum 0.690, mythic groups sum 0.168, total 0.858).
    Probabilities scaled by 0.90 so per-pack EV contribution is correct;
    strict_probs=False because the 10% retro-slot packs are handled separately.
    """
    q_r = _q("set:rvr", "rarity:rare",   "is:booster", "game:paper")
    q_m = _q("set:rvr", "rarity:mythic", "is:booster", "game:paper")
    return Slot(
        name="Main R/M showcase (fires 90% of packs, mythic 19.6%)",
        outcomes=[
            (0.90 * (1 - 0.196), QueryPool("rvr_main_r",
             q_r, unique="prints", price_field="usd")),
            (0.90 * 0.196, QueryPool("rvr_main_m",
             q_m, unique="prints", price_field="usd")),
        ],
        strict_probs=False,
    )


def slot_rvr_retro_rare() -> Slot:
    """
    Retro frame rare/mythic slot — fires in 10% of packs (variants 3 and 4),
    mutually exclusive with slot_rvr_main_rare.
    Cards: cn 302–407 (51 rares at 2/115 each, 13 mythics at 1/115 each).
    Mythic rate = 11.3% within this slot, verified from mtg.wtf sheet.
    Queried by cn range rather than frame:1997 for reliability.
    Excludes Collector-Booster-exclusive retro cards (different cn range).
    """
    q_retro_r = _q("set:rvr", "cn>=302", "cn<=407",
                   "rarity:rare",   "game:paper")
    q_retro_m = _q("set:rvr", "cn>=302", "cn<=407",
                   "rarity:mythic", "game:paper")
    return Slot(
        name="Retro R/M (fires 10% of packs, cn 302-407, mythic 11.3%)",
        outcomes=[
            (0.10 * (1 - 0.113), QueryPool("rvr_retro_r", q_retro_r,
             fallback=q_retro_r, unique="prints", price_field="usd")),
            (0.10 * 0.113, QueryPool("rvr_retro_m", q_retro_m,
             fallback=q_retro_m, unique="prints", price_field="usd")),
        ],
        strict_probs=False,
    )


def model_rvr_draft_box() -> ProductModel:
    """
    Built as a raw ProductModel rather than via model_from_config to avoid
    generating a third, incorrect main R/M slot from build_main_rm_slot().
    The wildcard and foil slots are reused from RVR_CONFIG via their builders.
    """
    return ProductModel(
        set_code="rvr", packs_per_box=36,
        slots=[
            build_wildcard_slot(RVR_CONFIG),
            # foil_rates already scaled to 33.3% frequency
            build_foil_slot(RVR_CONFIG),
            slot_rvr_mana_slot(),
            slot_rvr_main_rare(),
            slot_rvr_retro_rare(),
        ],
    )


# ============================================================
# LCI — The Lost Caverns of Ixalan
#
# Sources: mtg.wtf/pack/lci-set, lci-draft + Wizards collecting article.
#
# Set Booster (30/box):
#   1x cave_fullart_land (basic full-art, non-foil 80% / foil 20%)
#   3x common + 3x uncommon + 1x showcase_dfc_c_u  (≈$0, not modeled)
#   2x wildcard (any rarity; derived sheet rates: 7/1010 per rare, 7/3680 per mythic)
#      → wc_rm_rate ≈ 0.26 per slot (~70 rares × 7/1010 / 2 ≈ 24.3% + ~1.9% mythic)
#   1x rare_mythic (1/75 per rare, 1/150 per mythic; DEFAULT_MYTHIC_RATE approximates)
#   1x foil (rarity-weighted from card counts)
#   25% chance: 1x The List (SPG cn 1-10 at ~1/64, PLST remainder)
#
# Draft Booster (36/box):
#   1x cave_fullart_land (always non-foil in draft)
#   9-10x common + 1x dfc_common_uncommon + 3x uncommon (≈$0, not modeled)
#   1x rare_mythic (same sheet rates as set booster)
#   1/3 packs: traditional foil replaces 1 common
# ============================================================

LCI_SET_CONFIG = PlayBoosterConfig(
    set_code="lci", packs_per_box=30,
    mythic_rate=DEFAULT_MYTHIC_RATE,
    wc_rm_rate=0.26,        # per-slot RM rate; 2 WC slots give ~52% total RM across both
    wc_slots_per_pack=2,
    land_types=[LandTypeConfig(
        "cave_fullart", ["type:basic", "is:fullart"],
        rate=1.0, foil_rate=0.20, use_booster_filter=False,
    )],
)


def slot_lci_the_list() -> Slot:
    """
    The List fires in 25% of LCI Set Booster packs (4/20 + 1/20 variants).
    SPG cn 1-10 are LCI's Special Guests at ~1/64 per pack per the Wizards article.
    The remaining ~23.4% of packs draw from PLST.
    """
    q_spg = _q("set:spg", "cn>=1", "cn<=10", "game:paper")
    q_plst = _q("set:plst", "is:booster", "game:paper")
    p_spg = 1 / 64
    p_plst = 0.25 - p_spg
    return Slot(
        name="The List (LCI 25%: SPG 1-10 at 1/64, PLST remainder)",
        outcomes=[
            (0.75, 0.0),
            (p_spg,  QueryPool("lci_spg",       q_spg,  fallback=q_spg,
                               unique="prints", price_field="usd")),
            (p_plst, QueryPool("lci_list_plst",  q_plst, fallback=_q("set:plst", "game:paper"),
                               unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_lci_set_box() -> ProductModel:
    return model_from_config(LCI_SET_CONFIG, extra_slots=[slot_lci_the_list()])


def _draft_foil_slot(sc: str, p_foil: float,
                     p_fu: float, p_fr: float, p_fm: float) -> Slot:
    """Generic helper: foil replaces a common at rate p_foil; rarity split p_fu/p_fr/p_fm."""
    def _fp(label: str, rarity: str) -> QueryPool:
        q = _q(f"set:{sc}", f"rarity:{rarity}",
               "is:booster", "game:paper", "finish:foil")
        q_fb = _q(f"set:{sc}", f"rarity:{rarity}", "is:booster", "game:paper")
        return QueryPool(f"{sc}_draft_foil_{label}", q, fallback=q_fb,
                         unique="cards", price_field="usd_foil")
    return Slot(
        name=f"Foil (replaces common, {int(p_foil*100)}% of packs)",
        outcomes=[
            (1 - p_foil, 0.0),
            (p_foil * p_fu, _fp("u", "uncommon")),
            (p_foil * p_fr, _fp("r", "rare")),
            (p_foil * p_fm, _fp("m", "mythic")),
        ],
        strict_probs=True, renormalize=True,
    )


def model_lci_draft_box() -> ProductModel:
    """
    LCI Draft Booster (36/box).
    Every pack: 1 cave full-art land, 1 RM slot.
    1/3 packs: traditional foil (replaces a common).
    Foil rarity split approximated from foil_with_showcase sheet.
    """
    cfg = PlayBoosterConfig(
        set_code="lci", packs_per_box=36,
        mythic_rate=DEFAULT_MYTHIC_RATE,
        wc_rates=RarityRates(), wc_slots_per_pack=0,
        land_types=[LandTypeConfig(
            "cave_fullart", ["type:basic", "is:fullart"],
            rate=1.0, foil_rate=0.0, use_booster_filter=False,
        )],
    )
    return ProductModel(
        set_code="lci", packs_per_box=36,
        slots=[
            build_main_rm_slot(cfg),
            build_land_slot(cfg),
            _draft_foil_slot("lci", 1/3, p_fu=0.25, p_fr=0.09, p_fm=0.03),
        ],
    )


# ============================================================
# LTR — The Lord of the Rings: Tales of Middle-earth
#
# Sources: mtg.wtf/pack/ltr-set, ltr-draft + Wizards collecting article.
#
# Set Booster (30/box):
#   1x basic land (retro-style art, 15% foil — variants 3+4 = 15/20)
#   3x common + 3x uncommon + 1x common_uncommon_showcase  (≈$0, not modeled)
#   2x wildcard (7/1055 per rare, 7/2110 per mythic)
#      → wc_rm_rate ≈ 0.37 per slot (~101 rares × 7/1055 / 2 ≈ 33.5% + ~3.3% mythic)
#   1x rare_mythic (1/70 per rare, 1/140 per mythic → DEFAULT_MYTHIC_RATE approximates)
#   1x foil (rarity-weighted from card counts)
#   25% chance: 1x The List (PLST, ~75 cards at 1/300 each)
#
# Draft Booster (36/box):
#   1x basic land + 10 common + 3 uncommon + 1 RM
#   1/3 packs: traditional foil replaces 1 common
# ============================================================

LTR_SET_CONFIG = PlayBoosterConfig(
    set_code="ltr", packs_per_box=30,
    mythic_rate=DEFAULT_MYTHIC_RATE,
    wc_rm_rate=0.37,        # per-slot RM rate; ~101 rares × 7/1055 / 2 ≈ 33.5% + ~3.3% mythic
    wc_slots_per_pack=2,
    land_types=[LandTypeConfig(
        "basic", ["type:basic"],
        rate=1.0, foil_rate=0.15, use_booster_filter=False,
    )],
)


def _plst_the_list_slot(label: str, p_list: float) -> Slot:
    """Generic 'The List from PLST' slot at probability p_list."""
    q_plst = _q("set:plst", "is:booster", "game:paper")
    return Slot(
        name=f"The List (PLST, {int(p_list*100)}%)",
        outcomes=[
            (1.0 - p_list, 0.0),
            (p_list, QueryPool(f"{label}_list_plst", q_plst,
                               fallback=_q("set:plst", "game:paper"),
                               unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_ltr_set_box() -> ProductModel:
    return model_from_config(LTR_SET_CONFIG, extra_slots=[_plst_the_list_slot("ltr", 0.25)])


def model_ltr_draft_box() -> ProductModel:
    """
    LTR Draft Booster (36/box).
    Every pack: 1 basic land, 1 RM slot.
    1/3 packs: traditional foil replaces 1 common.
    """
    cfg = PlayBoosterConfig(
        set_code="ltr", packs_per_box=36,
        mythic_rate=DEFAULT_MYTHIC_RATE,
        wc_rates=RarityRates(), wc_slots_per_pack=0,
        land_types=[LandTypeConfig(
            "basic", ["type:basic"],
            rate=1.0, foil_rate=0.0, use_booster_filter=False,
        )],
    )
    return ProductModel(
        set_code="ltr", packs_per_box=36,
        slots=[
            build_main_rm_slot(cfg),
            build_land_slot(cfg),
            _draft_foil_slot("ltr", 1/3, p_fu=0.25, p_fr=0.08, p_fm=0.02),
        ],
    )


# ============================================================
# MOM — March of the Machine
#
# Sources: mtg.wtf/pack/mom-set, mom-draft + Wizards collecting article.
# MUL = March of the Machine Multiverse Legends (65 cards: 20U / 30R / 15M).
# MUL slot rates verified from mom-set sheet: 4/155 per U, 2/155 per R, 1/155 per M.
#   → P(U)=80/155 ≈ 51.6%, P(R)=60/155 ≈ 38.7%, P(M)=15/155 ≈ 9.7%
#
# Set Booster (30/box):
#   2x sfc_common + 2x sfc_uncommon + 1x dfc_c_u + 1x uncommon_battle (≈$0, not modeled)
#   2x wildcard (7/1160 per rare, 7/4000 per mythic)
#      → wc_rm_rate ≈ 0.23 per slot (~70 rares × 7/1160 / 2 ≈ 21.1% + ~1.75% mythic)
#   1x Multiverse Legend (MUL, always)
#   1x rare_mythic (1/70 per rare, 1/140 per mythic; DEFAULT_MYTHIC_RATE approximates)
#   1x foil: 96% traditional foil (rarity-weighted), 4% foil-etched MUL (Wizards article)
#   25% chance: 1x The List (PLST, 1/300 per card)
#   No basic land slot.
#
# Draft Booster (36/box):
#   1x basic_or_gainland + 7-8 sfc_common + DFC_c_u + uncommon_battle (≈$0, not modeled)
#   1x Multiverse Legend (MUL, always)
#   1x RM: 70% SFC | 19.29% battle | 10.71% DFC  (verified from variant weights in sheet)
#   1/3 packs: traditional foil (includes MUL; approximated as MOM rarity-weighted)
# ============================================================

MOM_SET_CONFIG = PlayBoosterConfig(
    set_code="mom", packs_per_box=30,
    mythic_rate=DEFAULT_MYTHIC_RATE,
    wc_rm_rate=0.23,        # per-slot RM rate; ~70 rares × 7/1160 / 2 ≈ 21.1% + ~1.75% mythic
    wc_slots_per_pack=2,
    land_types=[],          # no basic land slot in MOM set boosters
)

# MUL rarity probabilities (verified from mtg.wtf mom-set sheet)
_MUL_P_U = 80 / 155   # 20 uncommons × 4/155 each
_MUL_P_R = 60 / 155   # 30 rares     × 2/155 each
_MUL_P_M = 15 / 155   # 15 mythics   × 1/155 each


def slot_mul_legend(label_prefix: str) -> Slot:
    """
    Multiverse Legends dedicated slot (1 per pack, MOM set and draft).
    65 cards: 20U/30R/15M with unequal weights giving P(U)≈51.6%, P(R)≈38.7%, P(M)≈9.7%.
    """
    q_u = _q("set:mul", "rarity:uncommon", "game:paper")
    q_r = _q("set:mul", "rarity:rare",     "game:paper")
    q_m = _q("set:mul", "rarity:mythic",   "game:paper")
    return Slot(
        name="Multiverse Legends (MUL, 1 per pack)",
        outcomes=[
            (_MUL_P_U, QueryPool(f"{label_prefix}_mul_u",
             q_u, unique="prints", price_field="usd")),
            (_MUL_P_R, QueryPool(f"{label_prefix}_mul_r",
             q_r, unique="prints", price_field="usd")),
            (_MUL_P_M, QueryPool(f"{label_prefix}_mul_m",
             q_m, unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def slot_mom_set_foil() -> Slot:
    """
    MOM Set Booster foil slot: 96% traditional foil (any rarity, MOM), 4% foil-etched MUL.
    The 4% foil-etched rate is stated in the Wizards collecting article.
    """
    def _mom_foil(label: str, rarity: str) -> QueryPool:
        q = _q("set:mom", f"rarity:{rarity}",
               "is:booster", "game:paper", "finish:foil")
        q_fb = _q("set:mom", f"rarity:{rarity}", "is:booster", "game:paper")
        return QueryPool(f"mom_set_foil_{label}", q, fallback=q_fb,
                         unique="cards", price_field="usd_foil")
    # Rarity weights for the 96% regular foil (card-count-proportional, approx from MOM counts)
    # MOM: ~111c, 50u, 70r, 20m in SFC + battles + DFCs
    _p_fc, _p_fu, _p_fr, _p_fm = 0.55, 0.24, 0.17, 0.04
    q_mul_foil = _q("set:mul", "game:paper")
    return Slot(
        name="Foil (96% trad MOM rarity-weighted / 4% foil-etched MUL)",
        outcomes=[
            (0.96 * _p_fc, _mom_foil("c", "common")),
            (0.96 * _p_fu, _mom_foil("u", "uncommon")),
            (0.96 * _p_fr, _mom_foil("r", "rare")),
            (0.96 * _p_fm, _mom_foil("m", "mythic")),
            (0.04, QueryPool("mom_set_foil_etched_mul", q_mul_foil,
                             fallback=q_mul_foil, unique="prints", price_field="usd_foil")),
        ],
        strict_probs=True, renormalize=True,
    )


def model_mom_set_box() -> ProductModel:
    """
    Built as a raw ProductModel so we can use a custom composite foil slot
    (96% trad / 4% foil-etched MUL) instead of the generic build_foil_slot.
    """
    return ProductModel(
        set_code="mom", packs_per_box=30,
        slots=[
            build_main_rm_slot(MOM_SET_CONFIG),
            build_wildcard_slot(MOM_SET_CONFIG),
            slot_mom_set_foil(),
            slot_mul_legend("mom_set"),
            _plst_the_list_slot("mom", 0.25),
        ],
    )


def slot_mom_draft_rm() -> Slot:
    """
    MOM Draft rare/mythic slot: three mutually exclusive outcomes determined by
    pack variant (verified from mtg.wtf mom-draft sheet variant weights).
      70.00% — SFC rare/mythic (non-battle, non-DFC); DEFAULT_MYTHIC_RATE
      19.29% — Battle rare/mythic; mythic rate = 7/27 ≈ 25.9%
                (sheet: 2/27 per rare, 1/27 per mythic → N_r=10, N_m=7 sums to 27/27)
      10.71% — DFC rare/mythic; DEFAULT_MYTHIC_RATE
    """
    q_sfc_r = _q("set:mom", "rarity:rare",   "is:booster",
                 "-type:battle", "game:paper")
    q_sfc_m = _q("set:mom", "rarity:mythic", "is:booster",
                 "-type:battle", "game:paper")
    q_bat_r = _q("set:mom", "rarity:rare",   "type:battle", "game:paper")
    q_bat_m = _q("set:mom", "rarity:mythic", "type:battle", "game:paper")
    q_dfc_r = _q("set:mom", "rarity:rare",   "is:dfc",
                 "-type:battle", "game:paper")
    q_dfc_m = _q("set:mom", "rarity:mythic", "is:dfc",
                 "-type:battle", "game:paper")
    p_sfc, p_bat, p_dfc = 0.7000, 0.1929, 0.1071
    p_bat_m = 7 / 27   # derived from sheet rates 2/27 per rare, 1/27 per mythic
    return Slot(
        name="Main RM (70% SFC / 19.3% Battle / 10.7% DFC)",
        outcomes=[
            (p_sfc * (1 - DEFAULT_MYTHIC_RATE), QueryPool("mom_draft_sfc_r", q_sfc_r,
             unique="prints", price_field="usd")),
            (p_sfc * DEFAULT_MYTHIC_RATE,        QueryPool("mom_draft_sfc_m", q_sfc_m,
             unique="prints", price_field="usd")),
            (p_bat * (1 - p_bat_m),              QueryPool("mom_draft_bat_r", q_bat_r,
             unique="prints", price_field="usd")),
            (p_bat * p_bat_m,                    QueryPool("mom_draft_bat_m", q_bat_m,
             unique="prints", price_field="usd")),
            (p_dfc * (1 - DEFAULT_MYTHIC_RATE),  QueryPool("mom_draft_dfc_r", q_dfc_r,
             unique="prints", price_field="usd")),
            (p_dfc * DEFAULT_MYTHIC_RATE,         QueryPool("mom_draft_dfc_m", q_dfc_m,
             unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def model_mom_draft_box() -> ProductModel:
    """
    MOM Draft Booster (36/box).
    Every pack: 1 Multiverse Legend (MUL) + 1 RM (SFC/battle/DFC per variant).
    1/3 packs: traditional foil replaces 1 common (MOM rarity-weighted; MUL foils omitted).
    basic_or_gainland, DFC C/U, uncommon battle commons omitted (≈$0 EV).
    """
    return ProductModel(
        set_code="mom", packs_per_box=36,
        slots=[
            slot_mom_draft_rm(),
            slot_mul_legend("mom_draft"),
            _draft_foil_slot("mom", 1/3, p_fu=0.24, p_fr=0.09, p_fm=0.02),
        ],
    )


# ============================================================
# CMM — Commander Masters
#
# Sources: mtg.wtf/pack/cmm-draft, cmm-set + Wizards collecting article.
# Set composition: 130c / 135u / 135r (53 legendary, 82 nonlegendary) / 35m (15 leg, 20 nonleg).
# All rare/mythic includes borderless profile, frame-break, and borderless treatments.
#
# Legendary RM mythic rate = 15/(53+15) ≈ 22.1% (unweighted by treatment).
# Nonlegendary RM mythic rate = 20/(82+20) ≈ 19.6%.
#
# Draft Booster (24/box):
#   11-12x commons (with showcase) + 3-4x uncommons + 2x legendary uncommon (≈$0, not modeled)
#   1x legendary RM (always)
#   E[4/3] nonlegendary RM per pack:
#     10/18 and 2/18 variants → 1 nonleg_RM
#     5/18 and 1/18 variants  → 2 nonleg_RM
#     E = 1 + (5+1)/18 = 4/3 ≈ 1.333 per pack
#   1x traditional foil (always; rarity-weighted)
#   11.1% chance: Prismatic Piper (special slot) — $0 EV, not modeled
#
# Set Booster (24/box):
#   1x retro-frame basic land (20% foil, verified from foil_basic / 4-variants)
#   4x common + 1x common_uncommon_showcase + 2x nonleg_unc (≈$0, not modeled)
#   1x legendary uncommon (always) + 50%: extra leg_unc OR 50%: extra nonleg_RM
#   2x wildcard (7/1300 per rare, 7/3900 per mythic)
#      → wc_rm_rate ≈ 0.40 per slot (~135 rares × 7/1300 / 2 ≈ 36.3% + ~3.1% mythic)
#   1x legendary RM (always)
#   E[1.5] nonlegendary RM: 1 certain + 50% extra → E = 1.5
#   1x foil (rarity-weighted)
#   25% chance: The List (PLST, 1/300 per card)
# ============================================================

_CMM_LEG_MYTHIC_RATE = 15 / (53 + 15)   # ≈ 22.1%
_CMM_NONLEG_MYTHIC_RATE = 20 / (82 + 20)  # ≈ 19.6%


def slot_cmm_leg_rm() -> Slot:
    """Legendary rare/mythic: always fires once per pack in CMM Draft and Set."""
    q_r = _q("set:cmm", "rarity:rare",   "is:legendary",
             "is:booster", "game:paper")
    q_m = _q("set:cmm", "rarity:mythic",
             "is:legendary", "is:booster", "game:paper")
    return Slot(
        name="Legendary RM (1 per pack, mythic≈22.1%)",
        outcomes=[
            (1 - _CMM_LEG_MYTHIC_RATE, QueryPool("cmm_leg_r", q_r,
             unique="prints", price_field="usd")),
            (_CMM_LEG_MYTHIC_RATE,     QueryPool("cmm_leg_m", q_m,
             unique="prints", price_field="usd")),
        ],
        strict_probs=True,
    )


def slot_cmm_nonleg_rm(weight: float) -> Slot:
    """
    Nonlegendary rare/mythic. Weight encodes the expected count per pack:
      Draft: weight=4/3 (E[nonleg RM] from variant distribution)
      Set:   weight=1.5 (1 certain + 0.5 expected extra from 50%-variant)
    strict_probs=False because weight != 1 by design.
    """
    q_r = _q("set:cmm", "rarity:rare",   "-is:legendary",
             "is:booster", "game:paper")
    q_m = _q("set:cmm", "rarity:mythic",
             "-is:legendary", "is:booster", "game:paper")
    return Slot(
        name=f"Non-legendary RM (expected {weight:.3f}/pack, mythic≈19.6%)",
        outcomes=[
            (weight * (1 - _CMM_NONLEG_MYTHIC_RATE),
             QueryPool("cmm_nonleg_r", q_r, unique="prints", price_field="usd")),
            (weight * _CMM_NONLEG_MYTHIC_RATE,
             QueryPool("cmm_nonleg_m", q_m, unique="prints", price_field="usd")),
        ],
        strict_probs=False,
    )


def slot_cmm_draft_foil() -> Slot:
    """
    CMM Draft always has 1 traditional foil.
    Rarity probabilities approximated from foil sheet rates:
      3/730 per common × ~130 cards  ≈ 53.4%
      1/540 per uncommon × ~135      ≈ 25.0%
      1/700 per rare × ~135          ≈ 19.3%
      1/2100 per mythic × ~35        ≈  1.7%  (rest goes to special borderless)
    Fallback queries used since finish:foil filter may miss some borderless versions.
    """
    def _cf(label: str, rarity: str) -> QueryPool:
        q = _q("set:cmm", f"rarity:{rarity}",
               "is:booster", "game:paper", "finish:foil")
        q_fb = _q("set:cmm", f"rarity:{rarity}", "is:booster", "game:paper")
        return QueryPool(f"cmm_draft_foil_{label}", q, fallback=q_fb,
                         unique="cards", price_field="usd_foil")
    return Slot(
        name="Traditional foil (1 per CMM Draft pack)",
        outcomes=[
            (0.534, _cf("c", "common")),
            (0.250, _cf("u", "uncommon")),
            (0.193, _cf("r", "rare")),
            (0.017, _cf("m", "mythic")),
        ],
        strict_probs=True, renormalize=True,
    )


def model_cmm_draft_box() -> ProductModel:
    """
    CMM Draft Booster (24/box).
    Every pack: 1 legendary RM + E[4/3] nonlegendary RM + 1 foil.
    Commons/uncommons and Prismatic Piper (≈$0) not modeled.
    """
    return ProductModel(
        set_code="cmm", packs_per_box=24,
        slots=[
            slot_cmm_leg_rm(),
            slot_cmm_nonleg_rm(weight=4/3),
            slot_cmm_draft_foil(),
        ],
    )


CMM_SET_CONFIG = PlayBoosterConfig(
    set_code="cmm", packs_per_box=24,
    mythic_rate=DEFAULT_MYTHIC_RATE,   # used only for WC mythic split
    wc_rm_rate=0.40,                   # ~135 rares × 7/1300 / 2 ≈ 36.3% + ~3.1% mythic
    wc_slots_per_pack=2,
    land_types=[LandTypeConfig(
        "retro_basic", ["type:basic", "frame:1997"],
        rate=1.0, foil_rate=0.20, use_booster_filter=False,
    )],
)


def model_cmm_set_box() -> ProductModel:
    """
    CMM Set Booster (24/box).
    Built as a raw ProductModel to attach separate leg_RM, E[1.5] nonleg_RM,
    wildcard, foil, retro basic land, and The List slots.
    """
    return ProductModel(
        set_code="cmm", packs_per_box=24,
        slots=[
            build_wildcard_slot(CMM_SET_CONFIG),
            build_foil_slot(CMM_SET_CONFIG),
            build_land_slot(CMM_SET_CONFIG),
            slot_cmm_leg_rm(),
            slot_cmm_nonleg_rm(weight=1.5),
            _plst_the_list_slot("cmm", 0.25),
        ],
    )


# ============================================================
# Reporting helper
# ============================================================

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


# ============================================================
# Registry — maps (SET_CODE, kind) -> factory function
# To add a new set: write a model_xxx() above, add one line here.
# ============================================================

MODEL_REGISTRY: dict[tuple[str, str], Callable[[], "ProductModel"]] = {
    ("OTJ", "box"):       model_otj_play_box,
    ("WOE", "box"):       model_woe_set_box,
    ("WOE", "draft_box"): model_woe_draft_box,
    ("MH3", "box"):       model_mh3_play_box,
    ("ECL", "box"):       model_ecl_play_box,
    ("TLA", "box"):       model_tla_play_box,
    ("BLB", "box"):       model_blb_play_box,
    ("DSK", "box"):       model_dsk_play_box,
    ("DFT", "box"):       model_dft_play_box,
    ("FDN", "box"):       model_fdn_play_box,
    ("FIN", "box"):       model_fin_play_box,
    ("EOE", "box"):       model_eoe_play_box,
    ("TDM", "box"):       model_tdm_play_box,
    ("INR", "box"):       model_inr_play_box,
    ("SPM", "box"):       model_spm_play_box,
    # New sets (Play Booster era)
    ("ACR", "box"):        model_acr_beyond_box,
    ("MKM", "box"):        model_mkm_play_box,
    ("RVR", "draft_box"):  model_rvr_draft_box,
    # Pre-Play-Booster sets
    ("LCI", "box"):        model_lci_set_box,
    ("LCI", "draft_box"):  model_lci_draft_box,
    ("LTR", "box"):        model_ltr_set_box,
    ("LTR", "draft_box"):  model_ltr_draft_box,
    ("MOM", "box"):        model_mom_set_box,
    ("MOM", "draft_box"):  model_mom_draft_box,
    ("CMM", "box"):        model_cmm_set_box,
    ("CMM", "draft_box"):  model_cmm_draft_box,
}


def model_for_code(code: str, kind: str = "box") -> "ProductModel | None":
    """Return a fresh ProductModel for the given set-code + product kind, or None."""
    key = (code.strip().upper(), kind.strip().lower())
    factory = MODEL_REGISTRY.get(key)
    return factory() if factory else None
