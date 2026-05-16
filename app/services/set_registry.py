# app/services/set_registry.py
"""
Single source of truth for MTG set definitions.

Adding a new standard Play Booster set requires ONE entry here and nothing else.
Both the catalog (eBay query, product metadata) and the EV model (slot config,
bonus sheet) are derived automatically from this definition.

Complex sets with non-standard slot structures (MH3, OTJ, WOE, ECL, TLA) are
NOT in this registry — their models are hand-crafted in ev_core.py and
registered manually in EV_CORE_OVERRIDES at the bottom of this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PLAY_MYTHIC_RATE   = 1 / 7   # Play Booster era (MKM onward)
DRAFT_MYTHIC_RATE  = 1 / 8   # Draft Booster era (pre-2024)


# ---------------------------------------------------------------------------
# BonusSlot — describes a "replaces a common" bonus sheet
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BonusSlot:
    """
    Describes a bonus card slot that replaces one common at a given rate.

    Parameters
    ----------
    bonus_set   : Scryfall set code for the bonus sheet (e.g. "spg", "fca")
    cn_min      : Collector-number lower bound (inclusive)
    cn_max      : Collector-number upper bound (inclusive)
    rate        : Fraction of packs that contain the bonus (e.g. 1/64)
    label       : Short label used as the Scryfall pool key (e.g. "blb_spg")
    slot_name   : Human-readable slot description shown in EV reports
    """
    bonus_set:  str
    cn_min:     int
    cn_max:     int
    rate:       float
    label:      str
    slot_name:  str


# ---------------------------------------------------------------------------
# SetDef — everything needed to build catalog + EV model for a standard set
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SetDef:
    """
    Full definition for a standard Play Booster set.

    Fields
    ------
    set_code        : Upper-case Scryfall set code, e.g. "BLB"
    packs_per_box   : Number of packs in a play booster box
    ebay_query      : eBay Browse API search string for this product
    land_kind       : "basic" → only basic lands in land slot (most sets)
                      "any"   → type:land query (use if set has notable
                                nonbasic lands in the land slot)
    mythic_rate     : Fraction of main R/M slot that is mythic (default 1/8)
    wc_rm_rate      : Fraction of wildcard slot that is R or M (default 1/12)
    bonus           : Optional BonusSlot for sets with a bonus sheet
    product_key     : Catalog product key (default "play_box")
    product_label   : Catalog display label (default "Play Booster Box")
    product_kind    : eBay browse filter kind (default "play_box")
    """
    set_code:      str
    packs_per_box: int
    ebay_query:    str
    land_kind:     str = "basic"   # "basic" | "any"
    mythic_rate:   float = PLAY_MYTHIC_RATE   # 1/7 for Play Booster era
    wc_rm_rate:    float = 1 / 12
    bonus:         Optional[BonusSlot] = None
    product_key:   str = "play_box"
    product_label: str = "Play Booster Box"
    product_kind:  str = "play_box"


# ---------------------------------------------------------------------------
# SET_REGISTRY — add new standard sets here
# ---------------------------------------------------------------------------
# To add a set:
#   1. Append a SetDef entry below.
#   2. Done — catalog and EV model are auto-generated.
#
# For sets with custom slot structures (see EV_CORE_OVERRIDES below),
# add the hand-crafted model to ev_core.py and register it there instead.
# ---------------------------------------------------------------------------

SET_REGISTRY: dict[str, SetDef] = {s.set_code: s for s in [

    SetDef(
        set_code="BLB",
        packs_per_box=36,
        ebay_query="Bloomburrow play booster box",
        land_kind="any",
        bonus=BonusSlot(
            bonus_set="spg", cn_min=54, cn_max=63,
            rate=15 / 1000,
            label="blb_spg",
            slot_name="Special Guests (SPG 54-63, replaces common)",
        ),
    ),

    SetDef(
        set_code="DSK",
        packs_per_box=36,
        ebay_query="Duskmourn House of Horror play booster box",
        land_kind="basic",
        bonus=BonusSlot(
            bonus_set="spg", cn_min=64, cn_max=73,
            rate=1 / 64,
            label="dsk_spg",
            slot_name="Special Guests (SPG 64-73, replaces common)",
        ),
    ),

    SetDef(
        set_code="DFT",
        packs_per_box=30,
        ebay_query="Aetherdrift play booster box",
        land_kind="basic",
        bonus=BonusSlot(
            bonus_set="spg", cn_min=84, cn_max=93,
            rate=1 / 64,
            label="dft_spg",
            slot_name="Special Guests (SPG 84-93, replaces common)",
        ),
    ),

    SetDef(
        set_code="FDN",
        packs_per_box=36,
        ebay_query="MTG Foundations play booster box",
        land_kind="basic",
        bonus=BonusSlot(
            bonus_set="spg", cn_min=74, cn_max=83,
            rate=3 / 200,
            label="fdn_spg",
            slot_name="Special Guests (SPG 74-83, replaces common)",
        ),
    ),

    SetDef(
        set_code="FIN",
        packs_per_box=30,
        ebay_query="Final Fantasy play booster box MTG",
        land_kind="basic",
        bonus=BonusSlot(
            bonus_set="fca", cn_min=1, cn_max=9999,  # full set
            rate=1 / 3,
            label="fin_fca",
            slot_name="Through the Ages (FCA, replaces common)",
        ),
    ),

    SetDef(
        set_code="EOE",
        packs_per_box=30,
        ebay_query="Edge of Eternities play booster box MTG",
        land_kind="basic",
        bonus=BonusSlot(
            bonus_set="spg", cn_min=119, cn_max=128,
            rate=9 / 500,
            label="eoe_spg",
            slot_name="Special Guests (SPG 119-128, replaces common)",
        ),
    ),

    SetDef(
        set_code="TDM",
        packs_per_box=30,
        ebay_query="Tarkir Dragonstorm play booster box",
        land_kind="basic",
        bonus=BonusSlot(
            bonus_set="spg", cn_min=104, cn_max=113,
            rate=1 / 64,
            label="tdm_spg",
            slot_name="Special Guests (SPG 104-113, replaces common)",
        ),
    ),

    SetDef(
        set_code="INR",
        packs_per_box=36,
        ebay_query="Innistrad Remastered play booster box",
        land_kind="basic",
        # INR has a retro slot (CN 329-480), not a bonus-replaces-common slot.
        # model_inr_play_box() in ev_core.py handles this via EV_CORE_OVERRIDES.
    ),

    SetDef(
        set_code="SPM",
        packs_per_box=30,
        ebay_query="Marvel's Spider-Man play booster box MTG",
        land_kind="any",
        bonus=BonusSlot(
            bonus_set="mar", cn_min=1, cn_max=40,
            rate=1 / 24,
            label="spm_mar",
            slot_name="Source Material (MAR 1-40, replaces common)",
        ),
    ),

    # ── Add new sets below this line ────────────────────────────────────────
    # Example:
    # SetDef(
    #     set_code="XYZ",
    #     packs_per_box=36,
    #     ebay_query="Set Name play booster box MTG",
    #     land_kind="basic",
    #     bonus=BonusSlot(
    #         bonus_set="spg", cn_min=129, cn_max=138,
    #         rate=1/64,
    #         label="xyz_spg",
    #         slot_name="Special Guests (SPG 129-138, replaces common)",
    #     ),
    # ),
]}


# ---------------------------------------------------------------------------
# DraftBoosterDef — standard Draft Booster sets (2020–2024)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DraftBoosterDef:
    """
    Full definition for a standard Draft Booster set.

    Fields
    ------
    set_code        : Upper-case Scryfall set code, e.g. "THB"
    packs_per_box   : Number of packs in a draft booster box (usually 36)
    ebay_query      : eBay Browse API search string for this product
    land_kind       : "basic"         → type:basic in land slot (most sets)
                      "any"           → type:land (sets with notable nonbasic lands)
                      "fullart_basic" → type:basic is:fullart (ZNR, etc.)
                      "none"          → no land slot (e.g. Masters sets)
    mythic_rate     : Fraction of RM slot that is mythic (default 1/8)
    foil_rate       : Fraction of packs containing a foil (default 1/3)
    foil_p_u/r/m    : Conditional foil rarity split (given foil appears)
    bonus           : Optional BonusSlot for sets with a bonus sheet
    product_key     : Catalog product key (default "draft_box")
    product_label   : Catalog display label
    product_kind    : eBay browse filter kind (default "draft_box")
    """
    set_code:      str
    packs_per_box: int
    ebay_query:    str
    land_kind:     str   = "basic"
    mythic_rate:   float = DRAFT_MYTHIC_RATE
    foil_rate:     float = 1 / 3
    foil_p_u:      float = 0.27
    foil_p_r:      float = 0.10
    foil_p_m:      float = 0.03
    bonus:         Optional[BonusSlot] = None
    product_key:   str = "draft_box"
    product_label: str = "Draft Booster Box"
    product_kind:  str = "draft_box"


# ---------------------------------------------------------------------------
# DRAFT_REGISTRY — add standard draft booster sets here
# ---------------------------------------------------------------------------
# Sets with complex bonus sheets (STX, BRO, MH2, 2X2) have hand-crafted models
# in ev_core.py and are registered in MODEL_REGISTRY there instead.
# ---------------------------------------------------------------------------

DRAFT_REGISTRY: dict[str, DraftBoosterDef] = {s.set_code: s for s in [

    DraftBoosterDef(
        set_code="THB", packs_per_box=36,
        ebay_query="Theros Beyond Death draft booster box MTG",
    ),

    DraftBoosterDef(
        set_code="IKO", packs_per_box=36,
        ebay_query="Ikoria Lair of Behemoths draft booster box MTG",
        # Godzilla Series cards are non-Japanese box toppers only, not in individual packs.
    ),

    DraftBoosterDef(
        set_code="M21", packs_per_box=36,
        ebay_query="Core Set 2021 draft booster box MTG",
    ),

    DraftBoosterDef(
        set_code="ZNR", packs_per_box=36,
        ebay_query="Zendikar Rising draft booster box MTG",
        # Expeditions are box toppers only (not in individual draft packs).
        # Full-art basic lands in every pack land slot.
        land_kind="basic",
    ),

    DraftBoosterDef(
        set_code="KHM", packs_per_box=36,
        ebay_query="Kaldheim draft booster box MTG",
        # Snow-covered basics are type:basic — captured by default "basic" land query.
    ),

    DraftBoosterDef(
        set_code="AFR", packs_per_box=36,
        ebay_query="Adventures in the Forgotten Realms draft booster box MTG",
    ),

    DraftBoosterDef(
        set_code="MID", packs_per_box=36,
        ebay_query="Innistrad Midnight Hunt draft booster box MTG",
        # Eternal Night full-art basics are still type:basic — default query captures them.
    ),

    DraftBoosterDef(
        set_code="VOW", packs_per_box=36,
        ebay_query="Innistrad Crimson Vow draft booster box MTG",
    ),

    DraftBoosterDef(
        set_code="NEO", packs_per_box=36,
        ebay_query="Kamigawa Neon Dynasty draft booster box MTG",
    ),

    DraftBoosterDef(
        set_code="SNC", packs_per_box=36,
        ebay_query="Streets of New Capenna draft booster box MTG",
    ),

    DraftBoosterDef(
        set_code="DMU", packs_per_box=36,
        ebay_query="Dominaria United draft booster box MTG",
        # Stained-glass legends (DMU CN 287-327) are within the main DMU set;
        # every pack has a legendary creature due to high legend density — no dedicated
        # slot exists, so standard RM queries already capture the full price pool.
    ),

    # ── Add new sets below this line ────────────────────────────────────────
]}


# ---------------------------------------------------------------------------
# EV_CORE_OVERRIDES
# ---------------------------------------------------------------------------
# Set codes listed here have hand-crafted model factories in ev_core.py.
# The auto-builder in ev_core.model_for_code() will defer to those factories
# instead of building a generic model from SET_REGISTRY.
#
# Update this set when adding a new complex set to ev_core.py.
# ---------------------------------------------------------------------------
EV_CORE_OVERRIDES: frozenset[tuple[str, str]] = frozenset({
    ("MH3", "box"),
    ("OTJ", "box"),
    ("WOE", "box"),
    ("WOE", "draft_box"),
    ("ECL", "box"),
    ("TLA", "box"),
    ("INR", "box"),       # retro slot needs custom handling
    ("ONE", "box"),        # Set Booster
    ("STX", "box"),        # Set Booster (Mystical Archive bonus)
    ("STX", "draft_box"), # Mystical Archive bonus sheet
    ("BRO", "box"),        # Set Booster (Retro Artifact bonus)
    ("BRO", "draft_box"), # Retro Artifacts with schematic variant
    ("MH2", "draft_box"), # New-to-Modern reprint slot
    ("2X2", "draft_box"), # 2 R/M + 2 foils per pack, 24 packs/box
})


# ---------------------------------------------------------------------------
# Catalog helpers — consumed by catalog.py
# ---------------------------------------------------------------------------
def to_catalog_product(s: SetDef) -> dict:
    """Convert a SetDef into the catalog product dict shape."""
    return {
        "key":          s.product_key,
        "label":        s.product_label,
        "ebay_query":   s.ebay_query,
        "ev_set_code":  s.set_code,
        "ev_kind":      "box",
        "product_kind": s.product_kind,
        "default_sort": "price",
    }


def to_catalog_product_draft(d: DraftBoosterDef) -> dict:
    """Convert a DraftBoosterDef into the catalog product dict shape."""
    return {
        "key":          d.product_key,
        "label":        d.product_label,
        "ebay_query":   d.ebay_query,
        "ev_set_code":  d.set_code,
        "ev_kind":      "draft_box",
        "product_kind": d.product_kind,
        "default_sort": "price",
    }
