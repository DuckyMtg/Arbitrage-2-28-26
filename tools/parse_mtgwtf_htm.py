#!/usr/bin/env python3
"""
mtg.wtf HTM parser — diff against ev_core.py hand-crafted models.

Usage:
    python tools/parse_mtgwtf_htm.py <path/to/setplay_1.htm>

The script parses a mtg.wtf Play Booster HTM export and prints a structured
diff of every slot value that differs from the corresponding model in ev_core.py.
Unrecognised sets are listed with their raw slot data so you can add them.

Output is plain text (grep-friendly). Zero diff lines = model is accurate.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
    set_code: str  # ecl, spg, etc.

    @property
    def rate(self) -> float:
        return self.rate_num / self.rate_den


@dataclass
class PackVariant:
    num: int
    den: int
    sheets: list[str]  # e.g. ["7x common", "1x special_guest", ...]

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
    """Return (char_offset, header_text) for every h2-h4 tag."""
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
    """Extract rate-groups from a sheet section."""
    groups: list[SheetGroup] = []
    # Split by rate headers: "At rate NUM/DEN"
    parts = re.split(r"At rate (\d+)/(\d+)", section)
    i = 1
    while i < len(parts):
        num, den = int(parts[i]), int(parts[i + 1])
        block = parts[i + 2] if i + 2 < len(parts) else ""
        # Find all card CN references: /card/SET/CN (ignore letter suffixes like 124a)
        all_refs = re.findall(r"/card/([a-z]+)/(\d+)", block)
        # Deduplicate by (set, CN) – DFC cards have two face references
        seen: set[tuple[str, int]] = set()
        for s, cn_str in all_refs:
            key = (s, int(cn_str))
            seen.add(key)
        if seen:
            # Partition by set code in case SPG cards mixed in
            by_set: dict[str, list[int]] = {}
            for s, cn in seen:
                by_set.setdefault(s, []).append(cn)
            for s, cns in by_set.items():
                groups.append(SheetGroup(
                    rate_num=num, rate_den=den,
                    count=len(cns),
                    cn_min=min(cns), cn_max=max(cns),
                    set_code=s,
                ))
        i += 3
    return groups


def _parse_variants(header_block: str) -> list[PackVariant]:
    """Parse the pack variant list at the top of the HTM."""
    variants: list[PackVariant] = []
    # Match fraction like "216/275 (78.55%)" followed by <li> sheet items
    variant_re = re.compile(r"(\d+)/(\d+)\s*\([\d.]+%\)(.*?)(?=\d+/\d+\s*\(|$)", re.DOTALL)
    for m in variant_re.finditer(header_block):
        num, den = int(m.group(1)), int(m.group(2))
        block = m.group(3)
        sheets = [_strip_tags(li) for li in re.findall(r"<li>(.*?)</li>", block, re.DOTALL)]
        sheets = [s for s in sheets if s and "Sheet" in s]
        variants.append(PackVariant(num=num, den=den, sheets=sheets))
    return variants


def parse_htm(path: str | Path) -> ParsedHTM:
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    boundaries = _section_boundaries(content)

    # Detect set code from title line
    title_m = re.search(r"([A-Za-z\s]+) Play Booster has variants", content)
    title = title_m.group(0).split(" Play Booster")[0].strip() if title_m else "Unknown"
    # Guess set code from first /card/SET/ reference
    set_m = re.search(r"/card/([a-z]{2,5})/\d+", content)
    primary_set = set_m.group(1) if set_m else "unknown"

    # Parse pack variants (from content before the "Sheets" header)
    sheets_header_pos = next(
        (pos for pos, text in boundaries if text.strip().lower() == "sheets"), 0
    )
    header_block = content[:sheets_header_pos]
    variants = _parse_variants(header_block)

    # Parse each sheet
    sheet_names = [
        "common", "uncommon_fable", "wildcard", "rare_mythic_boosterfun",
        "foil", "non_foil_land", "foil_land", "special_guest",
    ]
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
    """Key numbers extracted from the HTM for one pack type."""
    set_code: str
    packs_per_box: Optional[int]  # not in HTM; must be looked up
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
    # wildcard sheet
    wc_common_count: int = 0
    wc_uncommon_count: int = 0
    wc_special_u_count: int = 0
    wc_rare_count: int = 0
    wc_mythic_count: int = 0
    wc_treat_rare_count: int = 0
    wc_treat_mythic_count: int = 0
    # foil sheet
    foil_common_count: int = 0
    foil_uncommon_count: int = 0
    foil_special_u_count: int = 0
    foil_rare_count: int = 0
    foil_mythic_count: int = 0
    foil_treat_rare_count: int = 0
    foil_treat_mythic_count: int = 0
    # bonus / SPG
    bonus_set: str = ""
    bonus_cn_min: int = 0
    bonus_cn_max: int = 0
    bonus_rate_num: int = 0
    bonus_rate_den: int = 275
    # land
    land_foil_rate_num: int = 0   # num of foil-land variants
    land_foil_rate_den: int = 275
    # reg uncommon / special-u CN boundaries
    reg_uncommon_cn_max: int = 0
    special_u_cn_min: int = 0


def _classify_rm_groups(groups: list[SheetGroup], sc: str) -> tuple[
    SheetGroup | None, SheetGroup | None, SheetGroup | None, SheetGroup | None
]:
    """
    Classify rare_mythic groups into (reg_rare, reg_mythic, treat_rare, treat_mythic).

    Strategy: group by rate_num (same numerator = same "base rate" pair).
    Within each pair, lower rate_den = rares, higher rate_den = mythics (2× rarer).
    Regular vs treatment split: whichever pair has lower cn_max = regular.
    """
    primary = [g for g in groups if g.set_code == sc]
    by_rate_num: dict[int, list[SheetGroup]] = {}
    for g in primary:
        by_rate_num.setdefault(g.rate_num, []).append(g)

    pairs: list[tuple[SheetGroup, SheetGroup]] = []  # (rare, mythic)
    for rn, grps in by_rate_num.items():
        if len(grps) != 2:
            continue
        grps.sort(key=lambda x: x.rate_den)  # lower den = rare
        pairs.append((grps[0], grps[1]))

    if len(pairs) != 2:
        return None, None, None, None

    # Pair with lower cn_max = regular set cards
    pairs.sort(key=lambda p: p[0].cn_max)
    reg_rare, reg_mythic = pairs[0]
    treat_rare, treat_mythic = pairs[1]
    return reg_rare, reg_mythic, treat_rare, treat_mythic


def _classify_sheet_groups(
    groups: list[SheetGroup], sc: str,
    reg_rare: SheetGroup, reg_mythic: SheetGroup,
    treat_rare: SheetGroup, treat_mythic: SheetGroup,
) -> dict:
    """
    Classify wildcard/foil groups using card counts from the rm sheet as keys.
    Returns dict with keys: common, uncommon, special_u, rare, mythic, treat_rare, treat_mythic.
    """
    primary = [g for g in groups if g.set_code == sc]
    result: dict[str, SheetGroup | None] = {k: None for k in
        ("common", "uncommon", "special_u", "rare", "mythic", "treat_rare", "treat_mythic")}

    # CN thresholds derived from rm
    reg_cn_boundary = max(reg_rare.cn_max, reg_mythic.cn_max)
    treat_cn_boundary = max(treat_rare.cn_min, treat_mythic.cn_min)

    for g in primary:
        if g.cn_max <= reg_cn_boundary:
            # Regular set cards — classify by count
            if g.count == reg_rare.count and result["rare"] is None:
                result["rare"] = g
            elif g.count == reg_mythic.count and result["mythic"] is None:
                result["mythic"] = g
            elif 75 <= g.count <= 85 and result["common"] is None:
                # Commons: almost always exactly 81 in a standard MTG set
                result["common"] = g
            elif g.count >= 86 and result["uncommon"] is None:
                result["uncommon"] = g
        elif g.cn_max > reg_cn_boundary:
            # Treatment or special-U cards
            if g.count == treat_rare.count and result["treat_rare"] is None:
                result["treat_rare"] = g
            elif g.count == treat_mythic.count and result["treat_mythic"] is None:
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
        if reg_rare and reg_mythic and treat_rare and treat_mythic:
            d.reg_rare_count = reg_rare.count
            d.reg_rare_cn_max = reg_rare.cn_max
            d.reg_rare_rate = f"{reg_rare.rate_num}/{reg_rare.rate_den}"
            d.reg_mythic_count = reg_mythic.count
            d.reg_mythic_cn_max = reg_mythic.cn_max
            d.reg_mythic_rate = f"{reg_mythic.rate_num}/{reg_mythic.rate_den}"
            d.treat_rare_count = treat_rare.count
            d.treat_rare_cn_min = treat_rare.cn_min
            d.treat_rare_rate = f"{treat_rare.rate_num}/{treat_rare.rate_den}"
            d.treat_mythic_count = treat_mythic.count
            d.treat_mythic_cn_min = treat_mythic.cn_min
            d.treat_mythic_rate = f"{treat_mythic.rate_num}/{treat_mythic.rate_den}"

            # wildcard
            wc = parsed.sheets.get("wildcard", [])
            if wc:
                cl = _classify_sheet_groups(wc, sc, reg_rare, reg_mythic, treat_rare, treat_mythic)
                if cl["rare"]:
                    d.wc_rare_count = cl["rare"].count
                if cl["mythic"]:
                    d.wc_mythic_count = cl["mythic"].count
                if cl["treat_rare"]:
                    d.wc_treat_rare_count = cl["treat_rare"].count
                if cl["treat_mythic"]:
                    d.wc_treat_mythic_count = cl["treat_mythic"].count
                if cl["common"]:
                    d.wc_common_count = cl["common"].count
                if cl["uncommon"]:
                    d.wc_uncommon_count = cl["uncommon"].count
                    d.reg_uncommon_cn_max = cl["uncommon"].cn_max
                if cl["special_u"]:
                    d.wc_special_u_count = cl["special_u"].count
                    d.special_u_cn_min = cl["special_u"].cn_min

            # foil
            foil = parsed.sheets.get("foil", [])
            if foil:
                cl = _classify_sheet_groups(foil, sc, reg_rare, reg_mythic, treat_rare, treat_mythic)
                if cl["rare"]:
                    d.foil_rare_count = cl["rare"].count
                if cl["mythic"]:
                    d.foil_mythic_count = cl["mythic"].count
                if cl["treat_rare"]:
                    d.foil_treat_rare_count = cl["treat_rare"].count
                if cl["treat_mythic"]:
                    d.foil_treat_mythic_count = cl["treat_mythic"].count
                if cl["common"]:
                    d.foil_common_count = cl["common"].count
                if cl["uncommon"]:
                    d.foil_uncommon_count = cl["uncommon"].count
                if cl["special_u"]:
                    d.foil_special_u_count = cl["special_u"].count

    # SPG / bonus slot
    spg = parsed.sheets.get("special_guest", [])
    if spg:
        for g in spg:
            d.bonus_set = g.set_code
            d.bonus_cn_min = g.cn_min
            d.bonus_cn_max = g.cn_max
            break

    # Bonus rate and land foil rate from pack variants
    total_den = parsed.variants[0].den if parsed.variants else 275
    spg_variants_num = sum(
        v.num for v in parsed.variants
        if any("special_guest" in s.lower() for s in v.sheets)
    )
    if spg_variants_num:
        d.bonus_rate_num = spg_variants_num
        d.bonus_rate_den = total_den

    # "foil_land" sheet (not "non_foil_land") — use exact word match
    foil_land_num = sum(
        v.num for v in parsed.variants
        if any(re.search(r"\bfoil_land\b", s) for s in v.sheets)
    )
    d.land_foil_rate_num = foil_land_num
    d.land_foil_rate_den = total_den

    return d


# ---------------------------------------------------------------------------
# Known model parameters extracted from ev_core.py for comparison
# ---------------------------------------------------------------------------

# Each entry: set_code -> dict of param_name -> value (as used in TreatmentPlayConfig)
KNOWN_MODELS: dict[str, dict] = {
    "ecl": {
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
        "bonus_cn_min": 129, "bonus_cn_max": 148,
        "bonus_rate": "5/275",
        "land_foil_rate": "55/275",  # 54+1 foil-land variants
    },
    "tla": {
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
        "bonus_cn_min": 1, "bonus_cn_max": 61,
        "bonus_rate": "5/130",
        "land_foil_rate": "1/5",
    },
}


# ---------------------------------------------------------------------------
# Diff report
# ---------------------------------------------------------------------------

def _frac(num: int, den: int) -> str:
    return f"{num}/{den}"


def diff_report(parsed: ParsedHTM, derived: DerivedSlot) -> list[str]:
    sc = parsed.set_code
    model = KNOWN_MODELS.get(sc)
    lines: list[str] = []

    lines.append(f"SET: {sc.upper()}  ({parsed.title})")
    lines.append(f"  Pack variants: {', '.join(f'{v.num}/{v.den}' for v in parsed.variants)}")
    lines.append("")

    if model is None:
        lines.append(f"  [NO MODEL FOUND for '{sc}' — raw derived values below]")
        lines.append(f"  reg rares:        {derived.reg_rare_count} @{derived.reg_rare_rate}  CN<={derived.reg_rare_cn_max}")
        lines.append(f"  reg mythics:      {derived.reg_mythic_count} @{derived.reg_mythic_rate}  CN<={derived.reg_mythic_cn_max}")
        lines.append(f"  treat rares:      {derived.treat_rare_count} @{derived.treat_rare_rate}  CN>={derived.treat_rare_cn_min}")
        lines.append(f"  treat mythics:    {derived.treat_mythic_count} @{derived.treat_mythic_rate}  CN>={derived.treat_mythic_cn_min}")
        lines.append(f"  wildcard rares:   {derived.wc_rare_count}")
        lines.append(f"  wildcard mythics: {derived.wc_mythic_count}")
        lines.append(f"  foil rares:       {derived.foil_rare_count}")
        lines.append(f"  foil mythics:     {derived.foil_mythic_count}")
        lines.append(f"  bonus:            {derived.bonus_set} CN {derived.bonus_cn_min}-{derived.bonus_cn_max}  rate={_frac(derived.bonus_rate_num, derived.bonus_rate_den)}")
        lines.append(f"  land foil rate:   {_frac(derived.land_foil_rate_num, derived.land_foil_rate_den)}")
        return lines

    def check(label: str, htm_val, model_val, fmt=str):
        htm_s = fmt(htm_val) if callable(fmt) else str(htm_val)
        mod_s = fmt(model_val) if callable(fmt) else str(model_val)
        if htm_s != mod_s:
            lines.append(f"  MISMATCH  {label:<35} HTM={htm_s!r:<20} model={mod_s!r}")
        else:
            lines.append(f"  ok        {label:<35} {htm_s}")

    # rare_mythic sheet
    check("reg_rare_count",    derived.reg_rare_count,     model.get("main_p_r_count"))
    check("reg_rare_rate",     derived.reg_rare_rate,      model.get("main_p_r_rate"))
    check("reg_rare_cn_max",   derived.reg_rare_cn_max,    model.get("reg_rare_cn_max"))
    check("reg_mythic_count",  derived.reg_mythic_count,   model.get("main_p_m_count"))
    check("reg_mythic_rate",   derived.reg_mythic_rate,    model.get("main_p_m_rate"))
    check("reg_mythic_cn_max", derived.reg_mythic_cn_max,  model.get("reg_mythic_cn_max"))
    check("treat_rare_count",  derived.treat_rare_count,   model.get("main_p_tr_count"))
    check("treat_rare_rate",   derived.treat_rare_rate,    model.get("main_p_tr_rate"))
    check("treat_rare_cn_min", derived.treat_rare_cn_min,  model.get("treat_rare_cn_min"))
    check("treat_mythic_count",derived.treat_mythic_count, model.get("main_p_tm_count"))
    check("treat_mythic_rate", derived.treat_mythic_rate,  model.get("main_p_tm_rate"))
    check("treat_mythic_cn_min",derived.treat_mythic_cn_min, model.get("treat_mythic_cn_min"))

    # wildcard
    check("wc_rare_count",     derived.wc_rare_count,      model.get("wc_p_r_count"))
    check("wc_mythic_count",   derived.wc_mythic_count,    model.get("wc_p_m_count"))
    check("wc_treat_rare_count", derived.wc_treat_rare_count, model.get("wc_p_tr_count"))
    check("wc_treat_mythic_count", derived.wc_treat_mythic_count, model.get("wc_p_tm_count"))
    check("reg_uncommon_cn_max", derived.reg_uncommon_cn_max, model.get("reg_uncommon_cn_max"))
    check("special_u_cn_min",  derived.special_u_cn_min,   model.get("special_u_cn_min"))

    # foil
    check("foil_rare_count",   derived.foil_rare_count,    model.get("foil_p_r_count"))
    check("foil_mythic_count", derived.foil_mythic_count,  model.get("foil_p_m_count"))
    check("foil_treat_rare_count", derived.foil_treat_rare_count, model.get("foil_p_tr_count"))
    check("foil_treat_mythic_count", derived.foil_treat_mythic_count, model.get("foil_p_tm_count"))

    # bonus / SPG
    check("bonus_cn_min",      derived.bonus_cn_min,       model.get("bonus_cn_min"))
    check("bonus_cn_max",      derived.bonus_cn_max,       model.get("bonus_cn_max"))
    check("bonus_rate",        _frac(derived.bonus_rate_num, derived.bonus_rate_den), model.get("bonus_rate"))

    # land foil rate
    htm_lfr = _frac(derived.land_foil_rate_num, derived.land_foil_rate_den)
    model_lfr = model.get("land_foil_rate", "?")
    check("land_foil_rate",    htm_lfr, model_lfr)

    mismatches = [l for l in lines if "MISMATCH" in l]
    if not mismatches:
        lines.append("\n  ✓ All checked values match model.")
    else:
        lines.append(f"\n  {len(mismatches)} MISMATCH(ES) found.")

    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python tools/parse_mtgwtf_htm.py <path/to/setplay_1.htm>")
        sys.exit(1)

    path = sys.argv[1]
    parsed = parse_htm(path)
    derived = derive_slot(parsed)
    report = diff_report(parsed, derived)
    print("\n".join(report))


if __name__ == "__main__":
    main()
