#!/usr/bin/env python3
"""
mtg.wtf HTM parser — diff against ev_core.py hand-crafted models.

Usage:
    python tools/parse_mtgwtf_htm.py <path/to/setplay_1.htm>
    python tools/parse_mtgwtf_htm.py --list        # show supported sets + URLs

The script parses a mtg.wtf Play Booster HTM export and prints a structured
diff of every slot value that differs from the corresponding model in ev_core.py.
Unknown sets print raw slot data so you can add a KNOWN_MODELS entry.

Output is plain text (grep-friendly). Zero diff lines = model is accurate.

HOW TO GET THE HTM FILES
    1. Run: python tools/parse_mtgwtf_htm.py --list
    2. Visit the URL shown for the set you want to verify
    3. Browser → Save Page As → "Web Page, HTML Only" → <setcode>play_1.htm
    4. Run: python tools/parse_mtgwtf_htm.py <setcode>play_1.htm
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# mtg.wtf URLs for supported sets (used by --list)
# ---------------------------------------------------------------------------

MTGWTF_BASE = "https://mtg.wtf/sealed?count%5B%5D=1&set%5B%5D="

MTGWTF_URLS: dict[str, str] = {
    # TreatmentPlayConfig sets
    "ecl": f"{MTGWTF_BASE}ecl-play",
    "tla": f"{MTGWTF_BASE}tla-play",
    # PlayBoosterConfig sets (current Standard + recent)
    "fin": f"{MTGWTF_BASE}fin-play",
    "tdm": f"{MTGWTF_BASE}tdm-play",
    "eoe": f"{MTGWTF_BASE}eoe-play",
    "spm": f"{MTGWTF_BASE}spm-play",
    "mkm": f"{MTGWTF_BASE}mkm-play",
    "dsk": f"{MTGWTF_BASE}dsk-play",
    "dft": f"{MTGWTF_BASE}dft-play",
    "fdn": f"{MTGWTF_BASE}fdn-play",
    "blb": f"{MTGWTF_BASE}blb-play",
    "inr": f"{MTGWTF_BASE}inr-play",
    "otj": f"{MTGWTF_BASE}otj-play",
}

# Standard sheet names used in most Play Booster HTMs.
# parse_htm detects additional sheets dynamically.
_STANDARD_SHEETS = frozenset({
    "common", "uncommon_fable", "wildcard", "rare_mythic_boosterfun",
    "foil", "non_foil_land", "foil_land",
})

# Sheet names that count as "bonus / replaces a common" slots
_BONUS_SHEET_NAMES = frozenset({
    "special_guest", "through_the_ages", "source_material",
    "retro", "breaking_news", "mystical_archive",
})


# ---------------------------------------------------------------------------
# HTM parsing
# ---------------------------------------------------------------------------

@dataclass
class SheetGroup:
    rate_num: int
    rate_den: int
    count: int
    cn_min: int
    cn_max: int
    set_code: str

    @property
    def rate(self) -> float:
        return self.rate_num / self.rate_den


@dataclass
class PackVariant:
    num: int
    den: int
    sheets: list[str]

    @property
    def prob(self) -> float:
        return self.num / self.den


@dataclass
class ParsedHTM:
    set_code: str
    title: str
    variants: list[PackVariant]
    sheets: dict[str, list[SheetGroup]]  # sheet_name -> groups


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _section_boundaries(content: str) -> list[tuple[int, str]]:
    return [
        (m.start(), _strip_tags(m.group(0)))
        for m in re.finditer(r"<h[2-4][^>]*>[^<]*</h[2-4]>", content, re.IGNORECASE)
    ]


def _get_section(content: str, boundaries: list[tuple[int, str]], name_fragment: str) -> str:
    for i, (pos, text) in enumerate(boundaries):
        if name_fragment.lower() in text.lower():
            header_end = content.index(">", pos) + 1
            end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(content)
            return content[header_end:end]
    return ""


def _parse_sheet_section(section: str, primary_set: str) -> list[SheetGroup]:
    groups: list[SheetGroup] = []
    parts = re.split(r"At rate (\d+)/(\d+)", section)
    i = 1
    while i < len(parts):
        num, den = int(parts[i]), int(parts[i + 1])
        block = parts[i + 2] if i + 2 < len(parts) else ""
        all_refs = re.findall(r"/card/([a-z]+)/(\d+)", block)
        seen: set[tuple[str, int]] = set()
        for s, cn_str in all_refs:
            seen.add((s, int(cn_str)))
        if seen:
            by_set: dict[str, list[int]] = {}
            for s, cn in seen:
                by_set.setdefault(s, []).append(cn)
            for s, cns in by_set.items():
                groups.append(SheetGroup(
                    rate_num=num, rate_den=den,
                    count=len(cns), cn_min=min(cns), cn_max=max(cns),
                    set_code=s,
                ))
        i += 3
    return groups


def _parse_variants(header_block: str) -> list[PackVariant]:
    variants: list[PackVariant] = []
    variant_re = re.compile(r"(\d+)/(\d+)\s*\([\d.]+%\)(.*?)(?=\d+/\d+\s*\(|$)", re.DOTALL)
    for m in variant_re.finditer(header_block):
        num, den = int(m.group(1)), int(m.group(2))
        block = m.group(3)
        sheets = [_strip_tags(li) for li in re.findall(r"<li>(.*?)</li>", block, re.DOTALL)]
        sheets = [s for s in sheets if s and "Sheet" in s]
        variants.append(PackVariant(num=num, den=den, sheets=sheets))
    return variants


def _detect_sheet_names(content: str, boundaries: list[tuple[int, str]]) -> list[str]:
    """Return all sheet names found in the HTM (e.g. 'common', 'special_guest', ...)."""
    names = []
    for _, text in boundaries:
        m = re.match(r"Sheet\s+(\S+)", text.strip(), re.IGNORECASE)
        if m:
            names.append(m.group(1).lower())
    return names


def parse_htm(path: str | Path) -> ParsedHTM:
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    boundaries = _section_boundaries(content)

    title_m = re.search(r"([A-Za-z\s']+) Play Booster has variants", content)
    title = title_m.group(0).split(" Play Booster")[0].strip() if title_m else "Unknown"

    set_m = re.search(r"/card/([a-z]{2,5})/\d+", content)
    primary_set = set_m.group(1) if set_m else "unknown"

    sheets_header_pos = next(
        (pos for pos, text in boundaries if text.strip().lower() == "sheets"), 0
    )
    header_block = content[:sheets_header_pos]
    variants = _parse_variants(header_block)

    # Detect all sheet names dynamically
    sheet_names = _detect_sheet_names(content, boundaries)

    sheets: dict[str, list[SheetGroup]] = {}
    for name in sheet_names:
        sec = _get_section(content, boundaries, f"Sheet {name}")
        if sec:
            sheets[name] = _parse_sheet_section(sec, primary_set)

    return ParsedHTM(set_code=primary_set, title=title, variants=variants, sheets=sheets)


# ---------------------------------------------------------------------------
# Derive slot parameters from parsed data
# ---------------------------------------------------------------------------

@dataclass
class DerivedSlot:
    set_code: str
    packs_per_box: Optional[int]
    # rare_mythic sheet
    reg_rare_count: int = 0
    reg_rare_cn_max: int = 0
    reg_rare_rate: str = ""
    reg_mythic_count: int = 0
    reg_mythic_cn_max: int = 0
    reg_mythic_rate: str = ""
    treat_rare_count: int = 0
    treat_rare_cn_min: int = 0
    treat_rare_rate: str = ""
    treat_mythic_count: int = 0
    treat_mythic_cn_min: int = 0
    treat_mythic_rate: str = ""
    has_treatments: bool = False
    # wildcard sheet
    wc_rare_count: int = 0
    wc_mythic_count: int = 0
    wc_treat_rare_count: int = 0
    wc_treat_mythic_count: int = 0
    reg_uncommon_cn_max: int = 0
    special_u_cn_min: int = 0
    # foil sheet
    foil_rare_count: int = 0
    foil_mythic_count: int = 0
    foil_treat_rare_count: int = 0
    foil_treat_mythic_count: int = 0
    # bonus slot (any non-standard sheet replacing a common)
    bonus_sheet_name: str = ""   # HTM sheet name
    bonus_set: str = ""          # set code of bonus cards (spg, fca, inr, mar, ...)
    bonus_cn_min: int = 0
    bonus_cn_max: int = 0
    bonus_rate_float: float = 0.0   # per-pack probability
    bonus_rate_den: int = 0         # HTM denominator (for display)
    bonus_rate_num: int = 0
    # land
    land_foil_rate_num: int = 0
    land_foil_rate_den: int = 275


def _classify_rm_groups(groups: list[SheetGroup], sc: str) -> tuple[
    Optional[SheetGroup], Optional[SheetGroup],
    Optional[SheetGroup], Optional[SheetGroup],
]:
    """
    Classify rare_mythic groups into (reg_rare, reg_mythic, treat_rare, treat_mythic).

    Groups by rate_num (same numerator = same rate pair).
    Within each pair: lower rate_den = rares (higher prob), higher = mythics (2× rarer).
    Regular vs treatment: pair with lower cn_max = regular.

    Returns (reg_rare, reg_mythic, None, None) for standard non-treatment sets (1 pair).
    Returns all four for treatment sets (2 pairs).
    Returns (None, None, None, None) if data is unclassifiable.
    """
    primary = [g for g in groups if g.set_code == sc]
    by_rate_num: dict[int, list[SheetGroup]] = {}
    for g in primary:
        by_rate_num.setdefault(g.rate_num, []).append(g)

    pairs: list[tuple[SheetGroup, SheetGroup]] = []
    for grps in by_rate_num.values():
        if len(grps) == 2:
            grps.sort(key=lambda x: x.rate_den)  # lower den = rares
            pairs.append((grps[0], grps[1]))

    if len(pairs) == 1:
        reg_rare, reg_mythic = pairs[0]
        return reg_rare, reg_mythic, None, None

    if len(pairs) == 2:
        pairs.sort(key=lambda p: p[0].cn_max)  # lower cn_max = regular
        reg_rare, reg_mythic = pairs[0]
        treat_rare, treat_mythic = pairs[1]
        return reg_rare, reg_mythic, treat_rare, treat_mythic

    return None, None, None, None


def _classify_sheet_groups(
    groups: list[SheetGroup], sc: str,
    reg_rare: SheetGroup, reg_mythic: SheetGroup,
    treat_rare: Optional[SheetGroup], treat_mythic: Optional[SheetGroup],
) -> dict:
    """
    Classify wildcard/foil groups using card counts from the rm sheet as keys.
    treat_rare / treat_mythic may be None for standard (non-treatment) sets.
    """
    primary = [g for g in groups if g.set_code == sc]
    result: dict[str, Optional[SheetGroup]] = {k: None for k in
        ("common", "uncommon", "special_u", "rare", "mythic", "treat_rare", "treat_mythic")}

    reg_cn_boundary = max(reg_rare.cn_max, reg_mythic.cn_max)

    for g in primary:
        if g.cn_max <= reg_cn_boundary:
            if g.count == reg_rare.count and result["rare"] is None:
                result["rare"] = g
            elif g.count == reg_mythic.count and result["mythic"] is None:
                result["mythic"] = g
            elif 75 <= g.count <= 85 and result["common"] is None:
                result["common"] = g
            elif g.count >= 86 and result["uncommon"] is None:
                result["uncommon"] = g
        else:
            if treat_rare and g.count == treat_rare.count and result["treat_rare"] is None:
                result["treat_rare"] = g
            elif treat_mythic and g.count == treat_mythic.count and result["treat_mythic"] is None:
                result["treat_mythic"] = g
            elif result["special_u"] is None:
                result["special_u"] = g
    return result


def derive_slot(parsed: ParsedHTM) -> DerivedSlot:
    sc = parsed.set_code
    d = DerivedSlot(set_code=sc, packs_per_box=None)

    rm = parsed.sheets.get("rare_mythic_boosterfun", [])
    if rm:
        reg_rare, reg_mythic, treat_rare, treat_mythic = _classify_rm_groups(rm, sc)
        if reg_rare and reg_mythic:
            d.reg_rare_count = reg_rare.count
            d.reg_rare_cn_max = reg_rare.cn_max
            d.reg_rare_rate = f"{reg_rare.rate_num}/{reg_rare.rate_den}"
            d.reg_mythic_count = reg_mythic.count
            d.reg_mythic_cn_max = reg_mythic.cn_max
            d.reg_mythic_rate = f"{reg_mythic.rate_num}/{reg_mythic.rate_den}"

            if treat_rare and treat_mythic:
                d.has_treatments = True
                d.treat_rare_count = treat_rare.count
                d.treat_rare_cn_min = treat_rare.cn_min
                d.treat_rare_rate = f"{treat_rare.rate_num}/{treat_rare.rate_den}"
                d.treat_mythic_count = treat_mythic.count
                d.treat_mythic_cn_min = treat_mythic.cn_min
                d.treat_mythic_rate = f"{treat_mythic.rate_num}/{treat_mythic.rate_den}"

            for sheet_name in ("wildcard", "foil"):
                sheet = parsed.sheets.get(sheet_name, [])
                if not sheet:
                    continue
                cl = _classify_sheet_groups(
                    sheet, sc, reg_rare, reg_mythic, treat_rare, treat_mythic
                )
                if sheet_name == "wildcard":
                    if cl["rare"]:
                        d.wc_rare_count = cl["rare"].count
                    if cl["mythic"]:
                        d.wc_mythic_count = cl["mythic"].count
                    if cl["treat_rare"]:
                        d.wc_treat_rare_count = cl["treat_rare"].count
                    if cl["treat_mythic"]:
                        d.wc_treat_mythic_count = cl["treat_mythic"].count
                    if cl["uncommon"]:
                        d.reg_uncommon_cn_max = cl["uncommon"].cn_max
                    if cl["special_u"]:
                        d.special_u_cn_min = cl["special_u"].cn_min
                else:  # foil
                    if cl["rare"]:
                        d.foil_rare_count = cl["rare"].count
                    if cl["mythic"]:
                        d.foil_mythic_count = cl["mythic"].count
                    if cl["treat_rare"]:
                        d.foil_treat_rare_count = cl["treat_rare"].count
                    if cl["treat_mythic"]:
                        d.foil_treat_mythic_count = cl["treat_mythic"].count

    # Bonus slot: look for any non-standard sheet referenced in pack variants
    total_den = parsed.variants[0].den if parsed.variants else 275
    for sheet_name, groups in parsed.sheets.items():
        if sheet_name in _STANDARD_SHEETS:
            continue
        # Count variants that include this sheet
        bonus_num = sum(
            v.num for v in parsed.variants
            if any(re.search(rf"\b{re.escape(sheet_name)}\b", s) for s in v.sheets)
        )
        if bonus_num > 0:
            d.bonus_sheet_name = sheet_name
            d.bonus_rate_num = bonus_num
            d.bonus_rate_den = total_den
            d.bonus_rate_float = bonus_num / total_den
            # Get set code and CN range from the sheet's card data
            if groups:
                # Use the first (or only) group from the primary bonus set
                # Exclude the main set's own cards in case of mixed sheets
                bonus_groups = [g for g in groups if g.set_code != sc]
                if not bonus_groups:
                    bonus_groups = groups
                g0 = bonus_groups[0]
                d.bonus_set = g0.set_code
                d.bonus_cn_min = g0.cn_min
                d.bonus_cn_max = g0.cn_max
            break

    # For ECL/TLA: bonus in pack variants is labelled "special_guest"
    if not d.bonus_sheet_name:
        spg_num = sum(
            v.num for v in parsed.variants
            if any("special_guest" in s.lower() for s in v.sheets)
        )
        if spg_num:
            d.bonus_sheet_name = "special_guest"
            d.bonus_rate_num = spg_num
            d.bonus_rate_den = total_den
            d.bonus_rate_float = spg_num / total_den
            spg_groups = parsed.sheets.get("special_guest", [])
            if spg_groups:
                d.bonus_set = spg_groups[0].set_code
                d.bonus_cn_min = spg_groups[0].cn_min
                d.bonus_cn_max = spg_groups[0].cn_max

    # Land foil rate: count foil_land variants (exact word match to avoid non_foil_land)
    foil_land_num = sum(
        v.num for v in parsed.variants
        if any(re.search(r"\bfoil_land\b", s) for s in v.sheets)
    )
    d.land_foil_rate_num = foil_land_num
    d.land_foil_rate_den = total_den

    return d


# ---------------------------------------------------------------------------
# KNOWN_MODELS — expected parameters from ev_core.py
# ---------------------------------------------------------------------------
#
# model_type:
#   "treatment"    — TreatmentPlayConfig (ECL, TLA): has treat_rare/mythic groups
#   "play_booster" — PlayBoosterConfig (all other play booster sets)
#
# For "treatment" models: all rate values are stored as "NUM/DEN" strings.
# For "play_booster" models: bonus_rate_float is a float (compared within 0.3% tolerance).

KNOWN_MODELS: dict[str, dict] = {
    # ------------------------------------------------------------------
    # TreatmentPlayConfig sets
    # ------------------------------------------------------------------
    "ecl": {
        "model_type": "treatment",
        "packs_per_box": 36,
        "main_p_r_count": 65, "main_p_r_rate": "459/38000",
        "main_p_m_count": 22, "main_p_m_rate": "459/76000",
        "main_p_tr_count": 36, "main_p_tr_rate": "41/23500",
        "main_p_tm_count": 22, "main_p_tm_rate": "41/47000",
        "reg_rare_cn_max": 268, "reg_mythic_cn_max": 253,
        "treat_rare_cn_min": 285, "treat_mythic_cn_min": 284,
        "wc_p_r_count": 65, "wc_p_m_count": 22,
        "wc_p_tr_count": 36, "wc_p_tm_count": 22,
        "reg_uncommon_cn_max": 263, "special_u_cn_min": 331,
        "foil_p_r_count": 65, "foil_p_m_count": 22,
        "foil_p_tr_count": 36, "foil_p_tm_count": 22,
        "bonus_set": "spg", "bonus_cn_min": 129, "bonus_cn_max": 148,
        "bonus_rate_float": 5 / 275,
        "land_foil_rate": "55/275",
    },
    "tla": {
        "model_type": "treatment",
        "packs_per_box": 36,
        "main_p_r_count": 62, "main_p_r_rate": "463/35000",
        "main_p_m_count": 26, "main_p_m_rate": "463/70000",
        "main_p_tr_count": 40, "main_p_tr_rate": "37/23500",
        "main_p_tm_count": 28, "main_p_tm_rate": "37/47000",
        "reg_rare_cn_max": 278, "reg_mythic_cn_max": 262,
        "treat_rare_cn_min": 302, "treat_mythic_cn_min": 297,
        "wc_p_r_count": 62, "wc_p_m_count": 26,
        "wc_p_tr_count": 40, "wc_p_tm_count": 28,
        "reg_uncommon_cn_max": 281, "special_u_cn_min": 299,
        "foil_p_r_count": 62, "foil_p_m_count": 26,
        "foil_p_tr_count": 40, "foil_p_tm_count": 28,
        "bonus_set": "tle", "bonus_cn_min": 1, "bonus_cn_max": 61,
        "bonus_rate_float": 5 / 130,
        "land_foil_rate": "1/5",  # verified: 54+1 foil-land / (54+1+70+5) ~ need HTM
    },
    # ------------------------------------------------------------------
    # PlayBoosterConfig sets (bonus only; rare/mythic counts not stored
    # because they use renormalize=True and exact counts vary by set)
    # ------------------------------------------------------------------
    "blb": {
        "model_type": "play_booster",
        "packs_per_box": 36,
        "bonus_set": "spg", "bonus_cn_min": 54, "bonus_cn_max": 63,
        "bonus_rate_float": 15 / 1000,   # from slot_blb_special_guests
    },
    "dsk": {
        "model_type": "play_booster",
        "packs_per_box": 36,
        "bonus_set": "spg", "bonus_cn_min": 64, "bonus_cn_max": 73,
        "bonus_rate_float": 1 / 64,
    },
    "dft": {
        "model_type": "play_booster",
        "packs_per_box": 30,
        "bonus_set": "spg", "bonus_cn_min": 84, "bonus_cn_max": 93,
        "bonus_rate_float": 1 / 64,
    },
    "fdn": {
        "model_type": "play_booster",
        "packs_per_box": 36,
        "bonus_set": "spg", "bonus_cn_min": 74, "bonus_cn_max": 83,
        "bonus_rate_float": 3 / 200,
    },
    "fin": {
        "model_type": "play_booster",
        "packs_per_box": 30,
        "bonus_set": "fca",
        "bonus_cn_min": 0, "bonus_cn_max": 9999,   # no CN restriction — any FCA card
        "bonus_rate_float": 1 / 3,
    },
    "eoe": {
        "model_type": "play_booster",
        "packs_per_box": 30,
        "bonus_set": "spg", "bonus_cn_min": 119, "bonus_cn_max": 128,
        "bonus_rate_float": 9 / 500,
    },
    "tdm": {
        "model_type": "play_booster",
        "packs_per_box": 30,
        "bonus_set": "spg", "bonus_cn_min": 104, "bonus_cn_max": 113,
        "bonus_rate_float": 1 / 64,
    },
    "inr": {
        "model_type": "play_booster",
        "packs_per_box": 36,
        "bonus_set": "inr", "bonus_cn_min": 329, "bonus_cn_max": 480,
        "bonus_rate_float": 1.0,   # retro slot always fires
    },
    "spm": {
        "model_type": "play_booster",
        "packs_per_box": 30,
        "bonus_set": "mar", "bonus_cn_min": 1, "bonus_cn_max": 40,
        "bonus_rate_float": 1 / 24,
    },
    "mkm": {
        "model_type": "play_booster",
        "packs_per_box": 36,
        "bonus_set": "spg", "bonus_cn_min": 19, "bonus_cn_max": 28,
        "bonus_rate_float": 1 / 64,
    },
    "otj": {
        "model_type": "play_booster",
        "packs_per_box": 36,
        # OTJ has a DEDICATED OTP (Breaking News) slot that always fires (1.0),
        # PLUS a The-List slot at 1/5 (which itself contains SPG 29-38 at 1/64).
        # The parser will report the bonus sheet from the HTM; verify manually.
        "bonus_set": "otp",
        "bonus_cn_min": 0, "bonus_cn_max": 9999,   # no CN restriction
        "bonus_rate_float": 1.0,  # Breaking News fires every pack
    },
}


# ---------------------------------------------------------------------------
# Diff report
# ---------------------------------------------------------------------------

_BONUS_RATE_TOL = 0.003   # 0.3% tolerance for per-pack rate float comparison


def _frac(num: int, den: int) -> str:
    return f"{num}/{den}"


def _check(lines: list[str], label: str, htm_val, model_val) -> None:
    htm_s, mod_s = str(htm_val), str(model_val)
    if htm_s != mod_s:
        lines.append(f"  MISMATCH  {label:<38} HTM={htm_s!r:<20} model={mod_s!r}")
    else:
        lines.append(f"  ok        {label:<38} {htm_s}")


def diff_report(parsed: ParsedHTM, derived: DerivedSlot) -> list[str]:
    sc = parsed.set_code
    model = KNOWN_MODELS.get(sc)
    lines: list[str] = []

    lines.append(f"SET: {sc.upper()}  ({parsed.title})")
    variant_str = ", ".join(f"{v.num}/{v.den}" for v in parsed.variants)
    lines.append(f"  Pack variants: {variant_str}")
    bonus_info = (
        f"{derived.bonus_sheet_name or 'none'}  set={derived.bonus_set or '?'}"
        f"  CN {derived.bonus_cn_min}-{derived.bonus_cn_max}"
        f"  rate={derived.bonus_rate_num}/{derived.bonus_rate_den}"
        f"  ({derived.bonus_rate_float*100:.2f}%)"
        if derived.bonus_sheet_name else "none detected"
    )
    lines.append(f"  Bonus sheet:   {bonus_info}")
    lines.append("")

    if model is None:
        lines.append(f"  [NO MODEL FOUND for '{sc}' — raw derived values below]")
        lines.append(f"  reg rares:  {derived.reg_rare_count} @{derived.reg_rare_rate}  CN<={derived.reg_rare_cn_max}")
        lines.append(f"  reg mythics:{derived.reg_mythic_count} @{derived.reg_mythic_rate}  CN<={derived.reg_mythic_cn_max}")
        if derived.has_treatments:
            lines.append(f"  treat rares:{derived.treat_rare_count} @{derived.treat_rare_rate}  CN>={derived.treat_rare_cn_min}")
            lines.append(f"  treat myth: {derived.treat_mythic_count} @{derived.treat_mythic_rate}  CN>={derived.treat_mythic_cn_min}")
        lines.append(f"  wc rares:   {derived.wc_rare_count}   wc mythics: {derived.wc_mythic_count}")
        lines.append(f"  foil rares: {derived.foil_rare_count} foil mythics:{derived.foil_mythic_count}")
        lines.append(f"  bonus:      set={derived.bonus_set}  CN {derived.bonus_cn_min}-{derived.bonus_cn_max}")
        lines.append(f"              rate={derived.bonus_rate_float*100:.3f}%")
        if sc in MTGWTF_URLS:
            lines.append(f"\n  Add a KNOWN_MODELS entry for '{sc}' to enable diff checking.")
        return lines

    mtype = model.get("model_type", "treatment")

    # --- Treatment model: full slot-by-slot diff ---
    if mtype == "treatment":
        _check(lines, "reg_rare_count",       derived.reg_rare_count,     model.get("main_p_r_count"))
        _check(lines, "reg_rare_rate",         derived.reg_rare_rate,      model.get("main_p_r_rate"))
        _check(lines, "reg_rare_cn_max",       derived.reg_rare_cn_max,    model.get("reg_rare_cn_max"))
        _check(lines, "reg_mythic_count",      derived.reg_mythic_count,   model.get("main_p_m_count"))
        _check(lines, "reg_mythic_rate",       derived.reg_mythic_rate,    model.get("main_p_m_rate"))
        _check(lines, "reg_mythic_cn_max",     derived.reg_mythic_cn_max,  model.get("reg_mythic_cn_max"))
        _check(lines, "treat_rare_count",      derived.treat_rare_count,   model.get("main_p_tr_count"))
        _check(lines, "treat_rare_rate",       derived.treat_rare_rate,    model.get("main_p_tr_rate"))
        _check(lines, "treat_rare_cn_min",     derived.treat_rare_cn_min,  model.get("treat_rare_cn_min"))
        _check(lines, "treat_mythic_count",    derived.treat_mythic_count, model.get("main_p_tm_count"))
        _check(lines, "treat_mythic_rate",     derived.treat_mythic_rate,  model.get("main_p_tm_rate"))
        _check(lines, "treat_mythic_cn_min",   derived.treat_mythic_cn_min,model.get("treat_mythic_cn_min"))
        _check(lines, "wc_rare_count",         derived.wc_rare_count,      model.get("wc_p_r_count"))
        _check(lines, "wc_mythic_count",       derived.wc_mythic_count,    model.get("wc_p_m_count"))
        _check(lines, "wc_treat_rare_count",   derived.wc_treat_rare_count,  model.get("wc_p_tr_count"))
        _check(lines, "wc_treat_mythic_count", derived.wc_treat_mythic_count,model.get("wc_p_tm_count"))
        _check(lines, "reg_uncommon_cn_max",   derived.reg_uncommon_cn_max,  model.get("reg_uncommon_cn_max"))
        _check(lines, "special_u_cn_min",      derived.special_u_cn_min,     model.get("special_u_cn_min"))
        _check(lines, "foil_rare_count",       derived.foil_rare_count,    model.get("foil_p_r_count"))
        _check(lines, "foil_mythic_count",     derived.foil_mythic_count,  model.get("foil_p_m_count"))
        _check(lines, "foil_treat_rare_count", derived.foil_treat_rare_count, model.get("foil_p_tr_count"))
        _check(lines, "foil_treat_mythic_count",derived.foil_treat_mythic_count, model.get("foil_p_tm_count"))
        _check(lines, "bonus_set",             derived.bonus_set,          model.get("bonus_set"))
        _check(lines, "bonus_cn_min",          derived.bonus_cn_min,       model.get("bonus_cn_min"))
        _check(lines, "bonus_cn_max",          derived.bonus_cn_max,       model.get("bonus_cn_max"))

        # Rate: float comparison with tolerance
        model_rate = model.get("bonus_rate_float", 0.0)
        htm_rate = derived.bonus_rate_float
        diff = abs(htm_rate - model_rate)
        rate_label = f"bonus_rate ({derived.bonus_rate_num}/{derived.bonus_rate_den})"
        if diff > _BONUS_RATE_TOL:
            lines.append(
                f"  MISMATCH  {'bonus_rate':<38} "
                f"HTM={htm_rate*100:.3f}%  model={model_rate*100:.3f}%  "
                f"diff={diff*100:.3f}%"
            )
        else:
            lines.append(f"  ok        {'bonus_rate':<38} {htm_rate*100:.3f}%")

        # Land foil rate (string fraction)
        htm_lfr = _frac(derived.land_foil_rate_num, derived.land_foil_rate_den)
        _check(lines, "land_foil_rate", htm_lfr, model.get("land_foil_rate", "?"))

    # --- Play booster model: check bonus slot only ---
    else:
        lines.append("  [play_booster model — checking bonus slot only]")
        lines.append("")
        _check(lines, "bonus_set", derived.bonus_set, model.get("bonus_set"))

        # For FIN/OTJ/INR (no CN restriction), skip cn range check
        m_cn_min = model.get("bonus_cn_min", 0)
        m_cn_max = model.get("bonus_cn_max", 9999)
        if m_cn_min == 0 and m_cn_max == 9999:
            lines.append(f"  skip      bonus CN range (no restriction for {sc.upper()})")
        else:
            _check(lines, "bonus_cn_min", derived.bonus_cn_min, m_cn_min)
            _check(lines, "bonus_cn_max", derived.bonus_cn_max, m_cn_max)

        model_rate = model.get("bonus_rate_float", 0.0)
        htm_rate = derived.bonus_rate_float
        if model_rate >= 1.0:
            # Always-fires slot: just check we detected a bonus
            label = "bonus_rate (always fires)"
            if htm_rate >= 0.99:
                lines.append(f"  ok        {label:<38} {htm_rate*100:.1f}%")
            else:
                lines.append(
                    f"  MISMATCH  {label:<38} "
                    f"HTM={htm_rate*100:.2f}%  model=100% (expected always-fires)"
                )
        else:
            diff = abs(htm_rate - model_rate)
            if diff > _BONUS_RATE_TOL:
                lines.append(
                    f"  MISMATCH  {'bonus_rate':<38} "
                    f"HTM={htm_rate*100:.3f}%  model={model_rate*100:.3f}%  "
                    f"diff={diff*100:.3f}%"
                )
            else:
                lines.append(f"  ok        {'bonus_rate':<38} {htm_rate*100:.3f}%")

    mismatches = [l for l in lines if "MISMATCH" in l]
    if not mismatches:
        lines.append("\n  ✓ All checked values match model.")
    else:
        lines.append(f"\n  {len(mismatches)} MISMATCH(ES) found.")

    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _print_list() -> None:
    print("Supported sets and their mtg.wtf URLs:\n")
    print(f"  {'SET':<6}  {'MODEL TYPE':<12}  URL")
    print(f"  {'-'*6}  {'-'*12}  {'-'*55}")
    for sc, model in KNOWN_MODELS.items():
        mtype = model.get("model_type", "?")
        url = MTGWTF_URLS.get(sc, "(URL not mapped)")
        print(f"  {sc.upper():<6}  {mtype:<12}  {url}")
    print()
    print("How to download:")
    print("  1. Visit the URL above in your browser")
    print("  2. Save Page As → Web Page, HTML Only → <setcode>play_1.htm")
    print("  3. Run: python tools/parse_mtgwtf_htm.py <setcode>play_1.htm")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    if sys.argv[1] == "--list":
        _print_list()
        sys.exit(0)

    path = sys.argv[1]
    parsed = parse_htm(path)
    derived = derive_slot(parsed)
    report = diff_report(parsed, derived)
    print("\n".join(report))


if __name__ == "__main__":
    main()
