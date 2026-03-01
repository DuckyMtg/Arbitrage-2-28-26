"""
Unit tests for eBay box-matching heuristics.

Run with:
    pytest tests/test_ebay_heuristics.py -v

Add new titles to SHOULD_MATCH / SHOULD_NOT_MATCH as you encounter
edge cases in production.
"""
from app.services.ebay_core import (
    match_product_by_title,
    looks_like_box_listing,
    normalize_title,
    tokens,
    PRODUCTS,
)
import sys
import os
import pytest

# Allow running from project root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


WOE = PRODUCTS["WOE_SET_BOX"]


# ============================================================
# looks_like_box_listing — isolated unit tests
# ============================================================

def _box(title: str, packs: int = 30) -> bool:
    tn = normalize_title(title)
    toks = tokens(tn)
    return looks_like_box_listing(tn, toks, packs_per_box=packs)


class TestLooksLikeBoxListing:

    # --- Should be True ---

    def test_explicit_booster_box(self):
        assert _box("Wilds of Eldraine Set Booster Box")

    def test_explicit_booster_box_lowercase(self):
        assert _box("wilds of eldraine set booster box")

    def test_display_wording(self):
        assert _box("Wilds of Eldraine Set Booster Display")

    def test_display_wording_lowercase(self):
        assert _box("wilds of eldraine booster display")

    def test_pack_count_match(self):
        assert _box("Wilds of Eldraine 30 Packs Set Booster", packs=30)

    def test_pack_count_match_different_packs(self):
        assert _box("Some Set 36 Packs Booster", packs=36)

    def test_boxes_plural(self):
        assert _box("Wilds of Eldraine Set Booster Boxes Lot of 2")

    def test_box_with_qty(self):
        assert _box("Wilds of Eldraine Booster Box qty 2")

    def test_box_with_quantity(self):
        assert _box("Wilds of Eldraine Booster Box quantity 3")

    # --- Should be False ---

    def test_single_pack(self):
        assert not _box("Wilds of Eldraine Set Booster Pack")

    def test_wrong_pack_count(self):
        # 15 packs != 30 packs_per_box
        assert not _box("Wilds of Eldraine 15 Packs", packs=30)

    def test_no_box_indicators(self):
        assert not _box("Wilds of Eldraine Set Booster")

    def test_empty_title(self):
        assert not _box("")


# ============================================================
# match_product_by_title — full pipeline tests against WOE config
# ============================================================

# Titles that SHOULD match WOE_SET_BOX
SHOULD_MATCH = [
    # Standard phrasings
    "Magic The Gathering Wilds of Eldraine Set Booster Box",
    "MTG Wilds of Eldraine Set Booster Box Sealed",
    "Wilds of Eldraine Set Booster Box - Factory Sealed",
    "Wilds of Eldraine Set Booster Box x1",
    "Magic MTG Wilds of Eldraine WOE Set Booster Box New Sealed",

    # Display phrasing
    "Wilds of Eldraine Set Booster Display Box",

    # Pack count phrasing (30 packs)
    "Wilds of Eldraine Set Booster 30 Packs Box",

    # Multi-box lots (should still match — lots are allowed)
    "Wilds of Eldraine Set Booster Box Lot of 2",
    "2x Wilds of Eldraine Set Booster Box",
]

# Titles that SHOULD NOT match WOE_SET_BOX
SHOULD_NOT_MATCH = [
    # Single packs
    ("Wilds of Eldraine Set Booster Pack",            "single pack"),
    ("Magic WOE Set Booster Pack x4",                 "single packs"),
    ("MTG Wilds of Eldraine Booster Pack",             "single pack"),

    # Wrong product types
    ("Wilds of Eldraine Collector Booster Box",        "collector box"),
    ("Wilds of Eldraine Draft Booster Box",            "draft box"),
    ("Wilds of Eldraine Bundle",                       "bundle"),
    ("Wilds of Eldraine Fat Pack Bundle Box",          "fat pack"),

    # Cases (not boxes)
    ("Wilds of Eldraine Set Booster Box Case",         "case"),
    ("WOE Set Booster Case 6 Boxes",                   "case"),

    # Wrong language / region
    ("Wilds of Eldraine Set Booster Box Japanese",     "japanese"),
    ("WOE Set Booster Box JP",                         "jp language"),
    ("Eldraine Set Booster Box Deutsch",               "german"),

    # Opened / empty
    ("Wilds of Eldraine Set Booster Box Opened",       "opened"),
    ("WOE Set Booster Box Empty",                      "empty"),

    ("MTG WOE Set Booster Display", "abbreviation only, missing required tokens"),

    # Unrelated MTG product
    ("Wilds of Eldraine Commander Deck",               "commander deck"),
    ("Wilds of Eldraine Starter Kit",                  "starter kit"),

    # Completely unrelated
    ("Pokemon Scarlet Violet Booster Box",             "wrong game"),
    ("One Piece Booster Box",                          "wrong game"),
]


class TestMatchProductByTitle:

    @pytest.mark.parametrize("title", SHOULD_MATCH)
    def test_should_match(self, title):
        matched, reason = match_product_by_title(title, WOE)
        assert matched, (
            f"Expected MATCH but got REJECT\n"
            f"  title:  {title!r}\n"
            f"  reason: {reason}"
        )

    @pytest.mark.parametrize("title,description", SHOULD_NOT_MATCH)
    def test_should_not_match(self, title, description):
        matched, reason = match_product_by_title(title, WOE)
        assert not matched, (
            f"Expected REJECT ({description}) but got MATCH\n"
            f"  title:  {title!r}\n"
            f"  reason: {reason}"
        )


# ============================================================
# normalize_title — sanity checks
# ============================================================

class TestNormalizeTitle:

    def test_lowercases(self):
        assert normalize_title("WILDS OF ELDRAINE") == "wilds of eldraine"

    def test_strips_hyphens(self):
        assert "factory sealed" in normalize_title("Factory-Sealed")

    def test_collapses_whitespace(self):
        assert normalize_title("wilds  of   eldraine") == "wilds of eldraine"

    def test_empty_string(self):
        assert normalize_title("") == ""

    def test_smart_apostrophe(self):
        # Smart quote should normalize to plain apostrophe
        result = normalize_title("Wilds\u2019 of Eldraine")
        assert "\u2019" not in result
