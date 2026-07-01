"""Collector Booster EV models for 26 MTG sets.

Slot probabilities derive from mtg.wtf HTM pull-rate denominators combined with
Scryfall card-count data via rarity_counts().  Per-card rate convention:
  rr = probability of drawing a specific rare   (e.g. 1/70)
  mr = probability of drawing a specific mythic (e.g. 1/140)
  p_rare = n_rares * rr,  p_mythic = n_mythics * mr
Normalised when total deviates >5 % from 1.0.
"""
from __future__ import annotations
from typing import Callable

from app.services.ev_core import (
    ProductModel, Slot, QueryPool, _q,
    rarity_counts, DEFAULT_MYTHIC_RATE, PLAY_MYTHIC_RATE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qp(lbl: str, q: str, *, f: bool = True, fb: str | None = None,
        unique: str = "prints") -> QueryPool:
    return QueryPool(lbl, q, fallback=fb, unique=unique,
                     price_field="usd_foil" if f else "usd")


def _rp(sc: str, rr: float, mr: float, *, fallback: float = PLAY_MYTHIC_RATE
        ) -> tuple[float, float]:
    c = rarity_counts(sc)
    n_r, n_m = c.get("rare", 0), c.get("mythic", 0)
    if n_r + n_m == 0:
        return 1 - fallback, fallback
    p_r, p_m = n_r * rr, n_m * mr
    t = p_r + p_m
    if t > 0 and abs(t - 1.0) > 0.05:
        p_r, p_m = p_r / t, p_m / t
    return p_r, p_m


def _old(sc: str, rr: float, mr: float) -> tuple[float, float]:
    return _rp(sc, rr, mr, fallback=DEFAULT_MYTHIC_RATE)


def _fc(sc: str, n: int = 1) -> Slot:
    q = _q(f"set:{sc}", "rarity:common", "game:paper")
    return Slot(f"{n}x Foil Common", [(float(n), _qp(f"{sc}_fc", q))], strict_probs=False)


def _fu(sc: str, n: int = 1) -> Slot:
    q = _q(f"set:{sc}", "rarity:uncommon", "game:paper")
    return Slot(f"{n}x Foil Uncommon", [(float(n), _qp(f"{sc}_fu", q))], strict_probs=False)


def _fb(sc: str) -> Slot:
    q = _q(f"set:{sc}", "type:basic", "game:paper")
    return Slot("Foil Basic", [(1.0, _qp(f"{sc}_fb", q))], strict_probs=True)


def _fal(sc: str) -> Slot:
    q = _q(f"set:{sc}", "type:land", "is:fullart", "game:paper")
    fb = _q(f"set:{sc}", "type:land", "game:paper")
    return Slot("Foil Full-Art Land", [(1.0, _qp(f"{sc}_fal", q, fb=fb))], strict_probs=True)


def _fl(sc: str, extra: str = "") -> Slot:
    q = _q(f"set:{sc}", "type:land", "game:paper", extra)
    return Slot("Foil Land", [(1.0, _qp(f"{sc}_fl", q))], strict_probs=True)


def _frm(sc: str, rr: float, mr: float, *, old: bool = False, lbl: str = "Foil R/M",
         xr: str = "", xm: str = "", lang: str = "lang:en") -> Slot:
    """Foil rare/mythic from the base-frame pool (excludes showcase/extended/borderless)."""
    p_r, p_m = _old(sc, rr, mr) if old else _rp(sc, rr, mr)
    base = "-is:showcase -is:extendedart -is:borderless"
    qr = _q(f"set:{sc}", "rarity:rare", base, "game:paper", lang, xr)
    qm = _q(f"set:{sc}", "rarity:mythic", base, "game:paper", lang, xm)
    return Slot(lbl, [(p_r, _qp(f"{sc}_fr", qr)), (p_m, _qp(f"{sc}_fm", qm))],
                strict_probs=True, renormalize=True)


def _treat(sc: str, rr: float, mr: float, filt: str, *, foil: bool, old: bool = False,
           lbl: str, n: float = 1.0, tag: str = "", lang: str = "lang:en") -> Slot:
    """Special-treatment R/M slot (showcase, extended, etched, borderless, textured)."""
    p_r, p_m = _old(sc, rr, mr) if old else _rp(sc, rr, mr)
    tag = tag or sc + "_t"
    qr = _q(f"set:{sc}", "rarity:rare", filt, "game:paper", lang)
    qm = _q(f"set:{sc}", "rarity:mythic", filt, "game:paper", lang)
    return Slot(lbl, [(n * p_r, _qp(f"{tag}_r", qr, f=foil, unique="cards")),
                      (n * p_m, _qp(f"{tag}_m", qm, f=foil, unique="cards"))],
                strict_probs=(n == 1.0), renormalize=True)


def _bonus_rm(bsc: str, rr: float, mr: float, *, foil: bool = False, lbl: str,
              xr: str = "", xm: str = "", lang: str = "lang:en") -> Slot:
    """R/M from a bonus set with given per-card rates."""
    p_r, p_m = _old(bsc, rr, mr)
    qr = _q(f"set:{bsc}", "rarity:rare", "game:paper", lang, xr)
    qm = _q(f"set:{bsc}", "rarity:mythic", "game:paper", lang, xm)
    return Slot(lbl, [(p_r, _qp(f"{bsc}_br", qr, f=foil, unique="cards")),
                      (p_m, _qp(f"{bsc}_bm", qm, f=foil, unique="cards"))],
                strict_probs=True, renormalize=True)


def _cmd_var(sc: str, q_cmd: str, p_foil: float, lbl: str) -> Slot:
    """Commander variant: (1−p_foil) NF + p_foil foil from same pool."""
    return Slot(lbl,
                [(1 - p_foil, _qp(f"{sc}_cmd_nf", q_cmd, f=False)),
                 (p_foil,     _qp(f"{sc}_cmd_f",  q_cmd))],
                strict_probs=True)


def _bf_slot(sc: str, filt: str, *, foil: bool, n: float = 1.0, lbl: str,
             old: bool = False, lang: str = "lang:en") -> Slot:
    """BoosterFun combined R/M slot — avg-price approximation."""
    mr = DEFAULT_MYTHIC_RATE if old else PLAY_MYTHIC_RATE
    qr = _q(f"set:{sc}", "rarity:rare", filt, "game:paper", lang)
    qm = _q(f"set:{sc}", "rarity:mythic", filt, "game:paper", lang)
    return Slot(lbl, [(n * (1 - mr), _qp(f"{sc}_bfr", qr, f=foil, unique="cards")),
                      (n * mr, _qp(f"{sc}_bfm", qm, f=foil, unique="cards"))],
                strict_probs=(n == 1.0), renormalize=True)


# ---------------------------------------------------------------------------
# 2X2 — Double Masters 2022  (4 packs/box)
# Variants: 94.12 % foil_showcase_rm  /  5.88 % textured_rm
# ---------------------------------------------------------------------------
def model_2x2_collector_box() -> ProductModel:
    sc = "2x2"
    # 94.12% foil_showcase / 5.88% textured — combined variant slot
    p_fsc_r, p_fsc_m = _old(sc, 1 / 40, 1 / 80)
    p_tx_r,  p_tx_m  = _old(sc, 1 / 5,  1 / 5)
    q_fsc_r = _q(f"set:{sc}", "rarity:rare",   "is:borderless", "game:paper", "lang:en")
    q_fsc_m = _q(f"set:{sc}", "rarity:mythic", "is:borderless", "game:paper", "lang:en")
    q_tx_r  = _q(f"set:{sc}", "rarity:rare",   "finish:textured", "game:paper", "lang:en")
    q_tx_m  = _q(f"set:{sc}", "rarity:mythic", "finish:textured", "game:paper", "lang:en")
    var_slot = Slot(
        "Foil Borderless R/M (94.12%) / Textured R/M (5.88%)",
        [(0.9412 * p_fsc_r, _qp("2x2_fsc_r", q_fsc_r, unique="cards")),
         (0.9412 * p_fsc_m, _qp("2x2_fsc_m", q_fsc_m, unique="cards")),
         (0.0588 * p_tx_r,  _qp("2x2_tx_r",  q_tx_r,  unique="cards")),
         (0.0588 * p_tx_m,  _qp("2x2_tx_m",  q_tx_m,  unique="cards"))],
        strict_probs=True, renormalize=True,
    )
    q_sc_c = _q(f"set:{sc}", "rarity:common",   "is:borderless", "game:paper", "lang:en")
    q_sc_u = _q(f"set:{sc}", "rarity:uncommon", "is:borderless", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=4, slots=[
        _fc(sc, 5),
        _fu(sc, 2),
        Slot("2x Borderless C/U NF",   [(2.0, _qp("2x2_sc_c", q_sc_c, f=False)),
                                        (1.0, _qp("2x2_sc_u", q_sc_u, f=False))], strict_probs=False),
        Slot("2x Foil Borderless C/U", [(2.0, _qp("2x2_fsc_c", q_sc_c)),
                                        (1.0, _qp("2x2_fsc_u", q_sc_u))],          strict_probs=False),
        _frm(sc, 1 / 140, 1 / 280, old=True),
        _treat(sc, 1 / 40,  1 / 80,  "is:borderless", foil=False, old=True, lbl="Borderless R/M NF", tag="2x2_sc_nf"),
        _treat(sc, 1 / 140, 1 / 280, "is:etched",     foil=False, old=True, lbl="Etched R/M",        tag="2x2_eth"),
        var_slot,
    ])


# ---------------------------------------------------------------------------
# ACR — Assassin's Creed  (12 packs/box)
# Variants: 86.4 % foil_basic  /  13.6 % borderless_scene NF
# ---------------------------------------------------------------------------
def model_acr_collector_box() -> ProductModel:
    sc = "acr"
    p_bl_r, p_bl_m = _rp(sc, 2 / 11, 1 / 11)
    q_fb   = _q(f"set:{sc}", "type:basic", "game:paper", "lang:en")
    q_bl_r = _q(f"set:{sc}", "rarity:rare",   "is:borderless", "game:paper", "lang:en")
    q_bl_m = _q(f"set:{sc}", "rarity:mythic", "is:borderless", "game:paper", "lang:en")
    var_slot = Slot(
        "Foil Basic (86.4%) / Borderless Scene NF (13.6%)",
        [(0.864,           _qp("acr_fb",   q_fb)),
         (0.136 * p_bl_r,  _qp("acr_bl_r", q_bl_r, f=False)),
         (0.136 * p_bl_m,  _qp("acr_bl_m", q_bl_m, f=False))],
        strict_probs=True, renormalize=True,
    )
    q_fsc_u = _q(f"set:{sc}", "rarity:uncommon", "is:showcase", "game:paper", "lang:en")
    q_eth_u = _q(f"set:{sc}", "rarity:uncommon", "is:etched",   "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fu(sc, 3),
        Slot("Foil Showcase Uncommon", [(1.0, _qp("acr_fsc_u", q_fsc_u))], strict_probs=True),
        Slot("Etched Uncommon",        [(1.0, _qp("acr_eth_u", q_eth_u, f=False))], strict_probs=True),
        var_slot,
        _frm(sc, 1 / 39, 1 / 78),
        _treat(sc, 2 / 33, 1 / 33, "is:extendedart", foil=False, lbl="Nonfoil Extended R/M", tag="acr_ext_nf"),
        _treat(sc, 1 / 39, 1 / 78, "is:showcase",    foil=True,  lbl="Foil Showcase R/M",   tag="acr_fsc"),
        _treat(sc, 1 / 39, 1 / 78, "is:etched",      foil=False, lbl="Etched R/M",           tag="acr_eth"),
    ])


# ---------------------------------------------------------------------------
# BLB — Bloomburrow  (12 packs/box)
# Commander slot: 97.18 % NF  /  2.82 % foil
# ---------------------------------------------------------------------------
def model_blb_collector_box() -> ProductModel:
    sc = "blb"
    q_cmd = _q("set:blc", "rarity:rare", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 5),
        _fu(sc, 4),
        _fb(sc),
        _frm(sc, 1 / 70, 1 / 140),
        _cmd_var(sc, q_cmd, 0.0282, "Commander (97.18% NF / 2.82% foil)"),
        _treat(sc, 1 / 76,  1 / 152, "is:showcase", foil=False, n=2.0, lbl="2x Showcase R/M NF",  tag="blb_sc_nf"),
        _treat(sc, 1 / 163, 1 / 163, "is:showcase", foil=True,       lbl="Foil Showcase R/M",     tag="blb_fsc"),
    ])


# ---------------------------------------------------------------------------
# BRO — The Brothers' War  (12 packs/box)
# Retro-artifact / schematic variant (50/50 split, averaged).
# Foil transformers in ~10.7 % of packs; serialised in 0.2 %.
# ---------------------------------------------------------------------------
def model_bro_collector_box() -> ProductModel:
    sc = "bro"
    brrsc = "brr"
    # BOT Transformers: rarity/lang filters return 0 cards in Scryfall; combined pool (English-only set)
    q_tr = _q("set:bot", "game:paper")
    # 50 % brr_retro_artifact_rare_mythic / 50 % brr_schematic_rare_mythic (same sheet structure)
    # finish:nonfoil avoids averaging in foil variants when unique=prints is used
    brr_rm = _bonus_rm(brrsc, 2 / 75, 1 / 75, lbl="BRR Retro/Schematic R/M", xr="-is:serialized finish:nonfoil", xm="-is:serialized finish:nonfoil")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fb(sc),
        _fc(sc, 2),
        _fu(sc, 4),
        _frm(sc, 2 / 149, 1 / 149, old=True),
        _treat(sc,    2 / 125, 1 / 125, "is:extendedart", foil=False, old=True, lbl="Extended Art R/M",        tag="bro_ext"),
        _treat("brc", 2 / 61,  1 / 61,  "is:extendedart", foil=False, old=True, lbl="Extended Commander/Jump", tag="bro_cmd"),
        Slot("BRR Retro Artifact/Schematic Uncommon",
             [(1.0, _qp("brr_u", _q(f"set:{brrsc}", "rarity:uncommon", "game:paper", "lang:en"), f=False))],
             strict_probs=True),
        brr_rm,
        Slot("Foil BRR Retro/Schematic C/U",
             [(1.0, _qp("brr_fcu", _q(f"set:{brrsc}", "(rarity:common or rarity:uncommon)", "game:paper", "lang:en")))],
             strict_probs=True),
        Slot("Transformers (89.3% NF / 10.7% foil)",
             [(0.893, _qp("bro_tr_nf", q_tr, f=False, unique="cards")),
              (0.107, _qp("bro_ftr",   q_tr, f=True,  unique="cards"))],
             strict_probs=True, renormalize=True),
        _bf_slot(sc, "is:extendedart", foil=True, lbl="Foil Alt Art", old=True),
    ])


# ---------------------------------------------------------------------------
# CMM — Commander Masters  (4 packs/box)
# 4 variants: 76.8 % standard / 3.2 % textured / 19.2 % foil_ext_cmd / 0.8 % both
# ---------------------------------------------------------------------------
def model_cmm_collector_box() -> ProductModel:
    sc = "cmm"
    p_ext_r, p_ext_m = _old(sc, 2 / 63, 1 / 63)
    q_ext_r = _q(f"set:{sc}", "rarity:rare",   "is:extendedart", "game:paper")
    q_ext_m = _q(f"set:{sc}", "rarity:mythic", "is:extendedart", "game:paper")
    q_tx_r  = _q(f"set:{sc}", "rarity:rare",   "finish:textured", "game:paper")
    q_tx_m  = _q(f"set:{sc}", "rarity:mythic", "finish:textured", "game:paper")
    p_tx_r, p_tx_m = _old(sc, 1 / 10, 1 / 10)
    # Commander slot: 80 % NF extended_cmd / 20 % foil_extended_cmd
    cmd_slot = Slot(
        "Commander Extended Art (80% NF / 20% foil)",
        [(0.80 * p_ext_r, _qp("cmm_cmd_nf_r", q_ext_r, f=False)),
         (0.80 * p_ext_m, _qp("cmm_cmd_nf_m", q_ext_m, f=False)),
         (0.20 * p_ext_r, _qp("cmm_cmd_f_r",  q_ext_r)),
         (0.20 * p_ext_m, _qp("cmm_cmd_f_m",  q_ext_m))],
        strict_probs=True, renormalize=True,
    )
    # Foil_sc_rm (96.8 %) / textured (3.2 %)
    p_fsc_r, p_fsc_m = _old(sc, 1 / 43, 1 / 86)
    q_sc_r = _q(f"set:{sc}", "rarity:rare",   "is:borderless", "game:paper")
    q_sc_m = _q(f"set:{sc}", "rarity:mythic", "is:borderless", "game:paper")
    fsc_tx = Slot(
        "Foil Borderless R/M (96.8%) / Textured R/M (3.2%)",
        [(0.968 * p_fsc_r, _qp("cmm_fsc_r", q_sc_r)),
         (0.968 * p_fsc_m, _qp("cmm_fsc_m", q_sc_m)),
         (0.032 * p_tx_r,  _qp("cmm_tx_r",  q_tx_r, unique="cards")),
         (0.032 * p_tx_m,  _qp("cmm_tx_m",  q_tx_m, unique="cards"))],
        strict_probs=True, renormalize=True,
    )
    q_sc_c = _q(f"set:{sc}", "rarity:common",   "is:borderless", "game:paper")
    q_sc_u = _q(f"set:{sc}", "rarity:uncommon", "is:borderless", "game:paper")
    return ProductModel(set_code=sc, packs_per_box=4, slots=[
        _fc(sc, 4),
        _fu(sc, 2),
        _fb(sc),
        Slot("2x Borderless C/U NF",   [(1.0, _qp("cmm_sc_c", q_sc_c, f=False)),
                                        (1.0, _qp("cmm_sc_u", q_sc_u, f=False))], strict_probs=False),
        Slot("Foil Borderless C/U",    [(1.0, _qp("cmm_fsc_c", q_sc_c)),
                                        (1.0, _qp("cmm_fsc_u", q_sc_u))],          strict_probs=False),
        _frm(sc, 2 / 305, 1 / 305, old=True),
        _treat(sc, 1 / 43, 1 / 86, "is:borderless", foil=False, old=True, lbl="Borderless R/M NF", tag="cmm_sc_nf"),
        _treat(sc, 2 / 309, 1 / 309, "is:etched",    foil=False, old=True, lbl="Etched R/M",       tag="cmm_eth"),
        cmd_slot,
        fsc_tx,
    ])


# ---------------------------------------------------------------------------
# DFT — Aetherdrift  (12 packs/box)
# Commander slot: 94.75 % NF showcase_cmd / 5.25 % foil_showcase_cmd
# ---------------------------------------------------------------------------
def model_dft_collector_box() -> ProductModel:
    sc = "dft"
    q_cmd = _q(f"set:{sc}", "rarity:rare", "is:extendedart", "game:paper", "lang:en")
    q_cu  = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "is:extendedart", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 4),
        _fu(sc, 3),
        _fl(sc),
        Slot("Revved-Up C/U NF",   [(1.0, _qp("dft_rev_cu",  q_cu, f=False))], strict_probs=True),
        Slot("Foil Revved-Up C/U", [(1.0, _qp("dft_frev_cu", q_cu))],          strict_probs=True),
        _frm(sc, 1 / 70, 1 / 140),
        _cmd_var(sc, q_cmd, 0.0525, "Showcase Commander (94.75% NF / 5.25% foil)"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=False, n=2.0, lbl="2x BoosterFun R/M NF"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True, lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# DSK — Duskmourn: House of Horror  (12 packs/box)
# Commander slot: 93.94 % NF showcase_cmd / 6.06 % foil_showcase_cmd
# ---------------------------------------------------------------------------
def model_dsk_collector_box() -> ProductModel:
    sc = "dsk"
    q_cmd = _q(f"set:{sc}", "rarity:rare", "is:showcase", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 5),
        _fu(sc, 4),
        _fb(sc),
        _frm(sc, 1 / 70, 1 / 140),
        _cmd_var(sc, q_cmd, 0.0606, "Showcase Commander (93.94% NF / 6.06% foil)"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=False, n=2.0, lbl="2x BoosterFun R/M NF"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True, lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# ECL — Lorwyn Eclipsed  (12 packs/box, no variants)
# ECC = Eclipsed bonus set rare/mythic
# ---------------------------------------------------------------------------
def model_ecl_collector_box() -> ProductModel:
    sc = "ecl"
    eccsc = "ecc"
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 5),
        _fu(sc, 4),
        _fl(sc),
        _frm(sc, 1 / 76, 1 / 152),
        Slot("ECC NF R/M",
             [(1.0, _qp("ecc_rm", _q(f"set:{eccsc}", "(rarity:rare or rarity:mythic)", "game:paper", "lang:en"), f=False))],
             strict_probs=True),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=False, n=2.0, lbl="2x BoosterFun R/M NF"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True,       lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# EOE — Edge of Eternities  (12 packs/box)
# 3 variants: 66.53 % stellar_sights_nf / 22.34 % foil_stellar / 11.12 % galaxy_stellar
# ---------------------------------------------------------------------------
def model_eoe_collector_box() -> ProductModel:
    sc = "eoe"
    q_land = _q(f"set:{sc}", "type:land", "game:paper", "lang:en")
    q_cmd  = _q(f"set:{sc}", "rarity:rare", "is:extendedart", "game:paper", "lang:en")
    # Stellar Sights land slot weighted by variant
    stellar_slot = Slot(
        "Stellar Sights Land (66.5% NF / 22.3% foil / 11.1% galaxy foil)",
        [(0.665, _qp("eoe_ssl_nf", q_land, f=False)),
         (0.335, _qp("eoe_ssl_f",  q_land))],
        strict_probs=True,
    )
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 5),
        _fu(sc, 4),
        Slot("Celestial Basic Land", [(1.0, _qp("eoe_cbl", _q(f"set:{sc}", "type:basic", "game:paper")))],
             strict_probs=True),
        _frm(sc, 1 / 70, 1 / 140),
        Slot("Commander R/M", [(1.0, _qp("eoe_cmd", q_cmd, f=False))], strict_probs=True),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=False, lbl="BoosterFun R/M NF"),
        stellar_slot,
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True, lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# FDN — Foundations  (12 packs/box, no variants)
# 2x foil_rm + 2x showcase_rm + foil_boosterfun
# ---------------------------------------------------------------------------
def model_fdn_collector_box() -> ProductModel:
    sc = "fdn"
    p_r, p_m = _rp(sc, 1 / 70, 1 / 140)
    base = "-is:showcase -is:extendedart -is:borderless"
    qr = _q(f"set:{sc}", "rarity:rare",   base, "game:paper", "lang:en")
    qm = _q(f"set:{sc}", "rarity:mythic", base, "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 5),
        _fu(sc, 4),
        _fb(sc),
        Slot("Foil R/M #1", [(p_r, _qp("fdn_fr1_r", qr)), (p_m, _qp("fdn_fr1_m", qm))],
             strict_probs=True, renormalize=True),
        Slot("Foil R/M #2", [(p_r, _qp("fdn_fr2_r", qr)), (p_m, _qp("fdn_fr2_m", qm))],
             strict_probs=True, renormalize=True),
        _treat(sc, 1 / 93, 1 / 186, "is:extendedart", foil=False, lbl="Showcase R/M #1 NF", tag="fdn_sc1"),
        _treat(sc, 1 / 93, 1 / 186, "is:extendedart", foil=False, lbl="Showcase R/M #2 NF", tag="fdn_sc2"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True, lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# FIN — Final Fantasy  (12 packs/box)
# 4 variants: V1 37.68% (NF TTA, 3x NF BF RM), V2 37.68% (foil TTA, 3x NF BF RM),
#             V3 12.32% (NF TTA, 2x NF BF RM + fic_foil), V4 12.32% (foil TTA, same).
# foil_rare_mythic rates from HTM: 351/29600 rare, 49/8000 mythic.
# ---------------------------------------------------------------------------
def model_fin_collector_box() -> ProductModel:
    sc = "fin"
    bf_filt = "(is:showcase or is:extendedart or is:borderless)"
    q_tta = _q(f"set:{sc}", "(is:showcase or is:extendedart)", "(rarity:rare or rarity:mythic)", "game:paper", "lang:en")
    q_fic = _q(f"set:{sc}", "is:extendedart", "rarity:rare", "game:paper", "lang:en")
    # NF BF RM: 3× in 75.36% of packs, 2× in 24.64% → average weight 2.754
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 3),
        _fu(sc, 3),
        _bf_slot(sc, "(is:showcase or is:extendedart)", foil=False, lbl="BoosterFun C/U NF"),
        _bf_slot(sc, "(is:showcase or is:extendedart)", foil=True,  lbl="Foil BoosterFun C/U"),
        _fb(sc),
        _frm(sc, 351 / 29600, 49 / 8000),   # exact rates from HTM
        _bf_slot(sc, bf_filt, foil=False, n=2.754, lbl="~3x BoosterFun R/M NF"),
        Slot("Through the Ages (50% NF / 50% foil)",
             [(0.50, _qp("fin_tta_nf", q_tta, f=False)),
              (0.50, _qp("fin_tta_f",  q_tta))],
             strict_probs=True),
        Slot("FIC Foil R/M (24.64% bonus)",
             [(0.2464, _qp("fin_fic_f", q_fic)),
              (0.7536, 0.0)],
             strict_probs=True),
        _bf_slot(sc, bf_filt, foil=True, lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# INR — Innistrad Remastered  (12 packs/box)
# Variant A (7.6 %): showcase_cu + retro_cu  /  Variant B (92.4 %): 2× retro_cu
# ---------------------------------------------------------------------------
def model_inr_collector_box() -> ProductModel:
    sc = "inr"
    q_sc_cu  = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "is:showcase", "game:paper", "lang:en")
    q_ret_cu = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "frame:1997",  "game:paper", "lang:en")
    q_fsc_cu = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "is:showcase", "game:paper", "lang:en")
    # Variant-averaged C/U slot: 7.6 % showcase + weighted retro
    cu_slot = Slot(
        "C/U Special (7.6% showcase + retro)",
        [(0.076, _qp("inr_sc_cu",  q_sc_cu,  f=False)),
         (1.924, _qp("inr_ret_cu", q_ret_cu, f=False))],  # 0.076×1 + 0.924×2 retro cards
        strict_probs=False,
    )
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 4),
        _fu(sc, 3),
        _fb(sc),
        cu_slot,
        Slot("Foil Showcase C/U", [(1.0, _qp("inr_fsc_cu", q_fsc_cu))], strict_probs=True),
        _frm(sc, 1 / 84, 1 / 161),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=False, n=2.0, lbl="2x BoosterFun R/M NF"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True, lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# LCI — Lost Caverns of Ixalan  (12 packs/box)
# JW slot: 79.9 % NF / 19.6 % foil / 0.5 % emblem (0 EV).
# Neon-ink appears in 0.7 % of packs replacing foil_showcase_rm.
# ---------------------------------------------------------------------------
def model_lci_collector_box() -> ProductModel:
    sc = "lci"
    # REX NF and foil priced from their own finishes to avoid cross-treatment averaging
    q_rex_nf  = _q("set:rex", "game:paper", "-is:serialized", "finish:nonfoil")
    q_rex_f   = _q("set:rex", "game:paper", "-is:serialized", "finish:foil")
    q_neon = _q(f"set:{sc}", "rarity:rare", "frame:neon", "game:paper")
    # Jurassic World slot: NF 79.9 %, foil 19.6 %, emblem 0.5 % (no card value)
    jw_slot = Slot(
        "Jurassic World (79.9% NF / 19.6% foil / 0.5% emblem)",
        [(0.799, QueryPool("rex_nf", q_rex_nf, unique="cards", price_field="usd")),
         (0.196, QueryPool("rex_f",  q_rex_f,  unique="cards", price_field="usd_foil")),
         (0.005, 0.0)],
        strict_probs=True, renormalize=True,
    )
    q_sc_u  = _q(f"set:{sc}", "rarity:uncommon", "(is:showcase or is:borderless)", "game:paper", "lang:en")
    # -frame:neon excludes Neon Ink showcase (already priced in its own 0.7% slot)
    q_fsc_r = _q(f"set:{sc}", "rarity:rare",   "is:showcase", "game:paper", "-is:serialized", "-frame:neon", "lang:en")
    q_fsc_m = _q(f"set:{sc}", "rarity:mythic", "is:showcase", "game:paper", "-is:serialized", "-frame:neon", "lang:en")
    p_fsc_r, p_fsc_m = _old(sc, 2 / 171, 1 / 171)
    # foil_showcase_rm present in 99.3 % of packs; neon_ink replaces it in 0.7 %
    fsc_slot = Slot(
        "Foil Showcase R/M (99.3%) / Neon Ink (0.7%)",
        [(0.993 * p_fsc_r, _qp("lci_fsc_r", q_fsc_r, unique="cards")),
         (0.993 * p_fsc_m, _qp("lci_fsc_m", q_fsc_m, unique="cards")),
         (0.007,           _qp("lci_neon",   q_neon,  unique="cards"))],
        strict_probs=True, renormalize=True,
    )
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fal(sc),
        _fc(sc, 4),
        _fu(sc, 3),
        Slot("Foil Showcase/Borderless Uncommon", [(1.0, _qp("lci_sc_u", q_sc_u))], strict_probs=True),
        _frm(sc, 1 / 75, 1 / 150, old=True),
        _treat(sc,    1 / 39, 1 / 78, "is:extendedart", foil=False, old=True, lbl="Extended Main R/M NF",  tag="lci_ext_m"),
        _treat("lcc", 1 / 43, 1 / 86, "is:extendedart", foil=False, old=True, lbl="Extended Commander NF", tag="lci_ext_c"),
        _treat(sc, 1 / 4,  1 / 4,  "is:showcase",    foil=False, old=True, lbl="Showcase R/M NF",       tag="lci_sc"),
        jw_slot,
        fsc_slot,
    ])


# ---------------------------------------------------------------------------
# LTR — Lord of the Rings: Tales of Middle-earth  (12 packs/box)
# Sol Ring slot: 7.37 % / Surge Relic: 0.74 % / One Ring: ~0 %
# ---------------------------------------------------------------------------
def model_ltr_collector_box() -> ProductModel:
    sc = "ltr"
    q_sr  = _q("set:ltc", "sol ring", "game:paper", "lang:en")
    q_fsc_rm   = _q(f"set:{sc}", "rarity:rare",   "is:showcase", "game:paper", "lang:en")
    q_fsc_rm_m = _q(f"set:{sc}", "rarity:mythic", "is:showcase", "game:paper", "lang:en")
    p_fsc_r, p_fsc_m = _old(sc, 4 / 131, 2 / 131)
    sol_slot = Slot(
        "Foil Common #4 (91.8% foil_c) / Sol Ring (7.37%) / Surge Relic (0.74%) / One Ring (~0%)",
        [(0.9183, _qp("ltr_fc4",    _q(f"set:{sc}", "rarity:common", "game:paper", "lang:en"))),
         (0.0737, _qp("ltr_sol",    q_sr,  f=False)),
         (0.0073, _qp("ltr_surge",  _q(f"set:{sc}", "rarity:rare", "is:showcase", "game:paper", "lang:en")))],
        strict_probs=True, renormalize=True,
    )
    q_sc_u = _q(f"set:{sc}", "rarity:uncommon", "is:showcase", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 3),       # 4th foil_common handled by sol_slot variant below
        _fu(sc, 2),
        _fb(sc),
        _frm(sc, 1 / 70, 1 / 140, old=True),
        _treat(sc, 2 / 65,  1 / 65,  "is:extendedart", foil=False, old=True, lbl="Extended Core R/M NF",      tag="ltr_ext_c"),
        _treat(sc, 1 / 86,  1 / 172, "is:extendedart", foil=False, old=True, lbl="Extended Jumpstart R/M NF",  tag="ltr_ext_j"),
        Slot("Showcase Uncommon NF", [(1.0, _qp("ltr_sc_u_nf", q_sc_u, f=False))], strict_probs=True),
        _treat(sc, 2 / 61,  1 / 61,  "is:showcase",    foil=False, old=True, lbl="Showcase R/M NF",            tag="ltr_sc_rm"),
        _treat(sc, 8 / 179, 4 / 179, "is:borderless",  foil=False, old=True, lbl="Borderless Scene NF",        tag="ltr_bl"),
        Slot("Foil Showcase Uncommon", [(1.0, _qp("ltr_fsc_u", q_sc_u))], strict_probs=True),
        Slot("Foil Showcase R/M",
             [(p_fsc_r, _qp("ltr_fsc_r", q_fsc_rm,   unique="cards")),
              (p_fsc_m, _qp("ltr_fsc_m", q_fsc_rm_m, unique="cards"))],
             strict_probs=True, renormalize=True),
        sol_slot,
    ])


# ---------------------------------------------------------------------------
# MH2 — Modern Horizons 2  (12 packs/box, no variants)
# ---------------------------------------------------------------------------
def model_mh2_collector_box() -> ProductModel:
    sc = "mh2"
    q_sk_cu = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "is:showcase", "game:paper", "lang:en")
    q_eth_cu = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "is:etched", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 4),
        _fu(sc, 2),
        Slot("Sketch C/U",             [(1.0, _qp("mh2_sk_cu",  q_sk_cu, f=False))],  strict_probs=True),
        _treat(sc, 1 / 39, 1 / 39, "is:extendedart", foil=False, old=True, lbl="Extended Art Rare NF", tag="mh2_ext"),
        Slot("2x Foil BoosterFun C/U", [(2.0, _qp("mh2_fbf_cu", _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "(is:showcase or is:extendedart)", "game:paper", "lang:en")))], strict_probs=False),
        _treat(sc, 2 / 77, 1 / 77, "(is:showcase or is:extendedart or is:borderless)", foil=False, old=True, lbl="BoosterFun R/M NF", tag="mh2_bf_nf"),
        _treat(sc, 2 / 253, 1 / 253, "(is:showcase or is:extendedart or is:borderless)", foil=True, old=True, lbl="Foil BoosterFun R/M", tag="mh2_bf_f"),
        Slot("Etched Basic",       [(1.0, _qp("mh2_eth_b", _q(f"set:{sc}", "type:basic", "is:etched", "game:paper"), f=False))],  strict_probs=True),
        Slot("Etched C/U",         [(1.0, _qp("mh2_eth_cu", q_eth_cu, f=False))],  strict_probs=True),
        _treat(sc, 2 / 109, 1 / 109, "is:etched", foil=False, old=True, lbl="Etched R/M", tag="mh2_eth_rm"),
    ])


# ---------------------------------------------------------------------------
# MH3 — Modern Horizons 3  (12 packs/box)
# Commander slot: 91.19 % NF showcase_cmd / 8.81 % foil_showcase_cmd
# ---------------------------------------------------------------------------
def model_mh3_collector_box() -> ProductModel:
    sc = "mh3"
    q_cmd    = _q(f"set:{sc}", "rarity:rare", "is:borderless", "game:paper", "lang:en")
    q_ret_cu = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "frame:1997", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 4),
        _fu(sc, 3),
        _fl(sc, "is:fullart"),        # Eldrazi land
        Slot("Retro C/U NF",   [(1.0, _qp("mh3_ret_cu_nf", q_ret_cu, f=False))], strict_probs=True),
        Slot("Foil Retro C/U", [(1.0, _qp("mh3_fret_cu",   q_ret_cu))],          strict_probs=True),
        _frm(sc, 1 / 90, 1 / 180),
        _cmd_var(sc, q_cmd, 0.0881, "Showcase Commander (91.19% NF / 8.81% foil)"),
        _treat(sc, 1 / 83, 1 / 166, "is:extendedart", foil=False, n=2.0, lbl="2x Showcase R/M NF", tag="mh3_sc_nf"),
        _treat(sc, 1 / 83, 1 / 166, "is:extendedart", foil=True,       lbl="Foil Showcase R/M",    tag="mh3_fsc"),
    ])


# ---------------------------------------------------------------------------
# MKM — Murders at Karlov Manor  (12 packs/box)
# Commander slot: 91.5 % NF extended_cmd / 8.5 % foil_extended_cmd
# ---------------------------------------------------------------------------
def model_mkm_collector_box() -> ProductModel:
    sc = "mkm"
    q_cmd = _q("set:mkc", "rarity:rare", "game:paper", "lang:en")
    q_cu_sc = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "is:showcase", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fal(sc),
        _fc(sc, 4),
        _fu(sc, 3),
        Slot("Showcase C/U NF",   [(1.0, _qp("mkm_sc_cu_nf", q_cu_sc, f=False))], strict_probs=True),
        Slot("Foil Showcase C/U", [(1.0, _qp("mkm_fsc_cu",   q_cu_sc))],          strict_probs=True),
        _frm(sc, 1 / 80, 1 / 160),
        _treat(sc, 1 / 31, 1 / 62, "is:extendedart", foil=False, lbl="Extended Main R/M NF",   tag="mkm_ext_m"),
        _cmd_var(sc, q_cmd, 0.085, "Commander (91.5% NF / 8.5% foil)"),
        _treat(sc, 2 / 99, 1 / 99, "is:showcase",    foil=False, lbl="Showcase R/M NF",        tag="mkm_sc"),
        _treat(sc, 2 / 99, 1 / 99, "is:showcase",    foil=True,  lbl="Foil Showcase R/M",      tag="mkm_fsc"),
    ])


# ---------------------------------------------------------------------------
# MOM — March of the Machine  (12 packs/box)
# MUL showcase slot: NF traditional (56 %), etched (10.8 %), halo (7.5 %),
#                    serialised (0.4 %), praetor double-foil serialised (0.5 %)
# ---------------------------------------------------------------------------
def model_mom_collector_box() -> ProductModel:
    sc = "mom"
    # Each MUL treatment priced from its own finish to avoid cross-treatment averaging
    q_mul_r_nf   = _q("set:mul", "rarity:rare",   "game:paper", "finish:nonfoil", "lang:en")
    q_mul_m_nf   = _q("set:mul", "rarity:mythic", "game:paper", "finish:nonfoil", "lang:en")
    q_mul_r_eth  = _q("set:mul", "rarity:rare",   "game:paper", "finish:etched",  "lang:en")
    q_mul_m_eth  = _q("set:mul", "rarity:mythic", "game:paper", "finish:etched",  "lang:en")
    q_mul_r_halo = _q("set:mul", "rarity:rare",   "game:paper", "finish:foil", "-finish:etched", "lang:en")
    q_mul_m_halo = _q("set:mul", "rarity:mythic", "game:paper", "finish:foil", "-finish:etched", "lang:en")
    p_mul_r, p_mul_m = _old("mul", 2 / 155, 1 / 155)
    mul_slot = Slot(
        "MUL Foil Uncommon",
        [(1.0, _qp("mul_fu", _q("set:mul", "rarity:uncommon", "game:paper", "lang:en")))],
        strict_probs=True,
    )
    mul_rm_slot = Slot(
        "MUL R/M (traditional 56%/etched 10.8%/halo 24.6%)",
        [(0.5597 * p_mul_r, QueryPool("mul_trad_r",  q_mul_r_nf,   unique="cards", price_field="usd")),
         (0.5597 * p_mul_m, QueryPool("mul_trad_m",  q_mul_m_nf,   unique="cards", price_field="usd")),
         (0.1082 * p_mul_r, QueryPool("mul_eth_r",   q_mul_r_eth,  unique="cards", price_field="usd_foil")),
         (0.1082 * p_mul_m, QueryPool("mul_eth_m",   q_mul_m_eth,  unique="cards", price_field="usd_foil")),
         (0.2463 * p_mul_r, QueryPool("mul_halo_r",  q_mul_r_halo, unique="cards", price_field="usd_foil")),
         (0.2463 * p_mul_m, QueryPool("mul_halo_m",  q_mul_m_halo, unique="cards", price_field="usd_foil"))],
        strict_probs=True, renormalize=True,
    )
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fal(sc),
        _fc(sc, 5),
        _fu(sc, 2),
        mul_slot,
        _frm(sc, 1 / 60, 1 / 120, old=True),
        _treat(sc, 1 / 40, 1 / 80, "is:extendedart", foil=False, old=True, lbl="Extended Art Commander/Jump NF", tag="mom_ext"),
        _bf_slot(sc, "(is:showcase or is:borderless)", foil=False, old=True, lbl="Showcase Wild NF"),
        mul_rm_slot,
        _bf_slot(sc, "(is:showcase or is:borderless)", foil=True, old=True, lbl="Foil Showcase R/M"),
    ])


# ---------------------------------------------------------------------------
# ONE — Phyrexia: All Will Be One  (12 packs/box, no variants)
# Compleat Foil: 7-tier slot, approximated as combined phyrexian showcase pool
# ---------------------------------------------------------------------------
def model_one_collector_box() -> ProductModel:
    sc = "one"
    q_comp = _q(f"set:{sc}", "(is:showcase or finish:oil)", "game:paper", "lang:en")
    q_sc_cu = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "is:showcase", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        Slot("Foil Full-Art Basic", [(1.0, _qp("one_fab", _q(f"set:{sc}", "type:basic", "is:fullart", "game:paper")))], strict_probs=True),
        _fc(sc, 4),
        _fu(sc, 2),
        _frm(sc, 1 / 70, 1 / 140, old=True),
        _treat(sc, 1 / 29, 1 / 29, "is:extendedart", foil=False, old=True, lbl="Extended Art R/M NF", tag="one_ext"),
        _treat(sc, 2 / 61, 1 / 61, "is:extendedart", foil=False, old=True, lbl="Extended Commander/Jump NF", tag="one_cmd"),
        Slot("Showcase C/U NF",   [(1.0, _qp("one_sc_cu_nf", q_sc_cu, f=False))], strict_probs=True),
        Slot("Foil Showcase C/U", [(1.0, _qp("one_fsc_cu",   q_sc_cu))],          strict_probs=True),
        Slot("Compleat Foil R/M", [(1.0, _qp("one_comp",     q_comp))],            strict_probs=True),
        _treat(sc, 2 / 83,  1 / 83,  "is:showcase", foil=False, old=True, lbl="Showcase R/M NF",  tag="one_sc_nf"),
        _treat(sc, 1 / 80,  1 / 160, "is:showcase", foil=True,  old=True, lbl="Foil Showcase R/M", tag="one_fsc"),
    ])


# ---------------------------------------------------------------------------
# OTJ — Outlaws of Thunder Junction  (12 packs/box)
# Commander slot: 94.74 % NF / 5.26 % foil
# OTP = Breaking News bonus set
# ---------------------------------------------------------------------------
def model_otj_collector_box() -> ProductModel:
    sc = "otj"
    q_cmd = _q("set:otc", "rarity:rare", "game:paper", "lang:en")
    q_fin = _q(f"set:{sc}", "rarity:rare", "is:showcase", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 4),
        _fu(sc, 3),
        _fal(sc),
        Slot("OTP Uncommon NF",   [(1.0, _qp("otp_u_nf", _q("set:otp", "rarity:uncommon", "game:paper", "lang:en"), f=False))], strict_probs=True),
        Slot("Foil OTP Uncommon", [(1.0, _qp("otp_u_f",  _q("set:otp", "rarity:uncommon", "game:paper", "lang:en")))],          strict_probs=True),
        _frm(sc, 1 / 85, 1 / 170),
        _treat(sc, 2 / 201, 1 / 201, "is:showcase",    foil=False, lbl="Showcase R/M NF", tag="otj_sc"),
        _cmd_var(sc, q_cmd, 0.0526, "Commander (94.74% NF / 5.26% foil)"),
        _bonus_rm("otp", 2 / 75, 1 / 75, lbl="OTP R/M NF"),
        Slot("Finale / Extended NF",
             [(1.0, _qp("otj_fin", q_fin, f=False))],
             strict_probs=True),
    ])


# ---------------------------------------------------------------------------
# RVR — Ravnica Remastered  (12 packs/box)
# Variant: 99 % foil_showcase_rm / 1 % serialised
# ---------------------------------------------------------------------------
def model_rvr_collector_box() -> ProductModel:
    sc = "rvr"
    q_ret_cu = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "frame:1997", "game:paper", "lang:en")
    p_fsc_r, p_fsc_m = _old(sc, 2 / 199, 1 / 199)
    q_fsc_r = _q(f"set:{sc}", "rarity:rare",   "frame:1997", "game:paper", "-is:serialized", "lang:en")
    q_fsc_m = _q(f"set:{sc}", "rarity:mythic", "frame:1997", "game:paper", "-is:serialized", "lang:en")
    q_ser   = _q(f"set:{sc}", "is:serialized", "game:paper")
    var_slot = Slot(
        "Foil Showcase R/M (99%) / Serialised (1%)",
        [(0.99 * p_fsc_r,  _qp("rvr_fsc_r", q_fsc_r)),
         (0.99 * p_fsc_m,  _qp("rvr_fsc_m", q_fsc_m)),
         (0.01,            _qp("rvr_ser",    q_ser, f=False))],
        strict_probs=True, renormalize=True,
    )
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 4),
        _fu(sc, 3),
        _fl(sc, "rarity:rare -frame:1997 finish:foil"),
        Slot("2x Retro C/U NF",   [(2.0, _qp("rvr_ret_cu_nf", q_ret_cu, f=False))], strict_probs=False),
        Slot("Foil Retro C/U",    [(1.0, _qp("rvr_fret_cu",   q_ret_cu))],           strict_probs=True),
        _frm(sc, 1 / 70,  1 / 140, old=True),
        _treat(sc, 1 / 66, 1 / 132, "frame:1997",    foil=False, old=True, lbl="Retro R/M NF",      tag="rvr_ret"),
        _treat(sc, 2 / 67, 1 / 67,  "is:borderless", foil=False, old=True, lbl="Borderless R/M NF", tag="rvr_bl"),
        var_slot,
    ])


# ---------------------------------------------------------------------------
# SPM — Marvel's Spider-Man  (12 packs/box)
# Source material: 75 % NF / 25 % foil
# ---------------------------------------------------------------------------
def model_spm_collector_box() -> ProductModel:
    sc = "spm"
    q_src = _q(f"set:{sc}", "(is:showcase or is:extendedart)", "rarity:rare", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 5),
        _fu(sc, 4),
        _fl(sc),
        _frm(sc, 1 / 68, 1 / 136),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=False, n=2.0, lbl="2x BoosterFun R/M NF"),
        Slot("Source Material (75% NF / 25% foil)",
             [(0.75, _qp("spm_src_nf", q_src, f=False)),
              (0.25, _qp("spm_src_f",  q_src))],
             strict_probs=True),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True, lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# STX — Strixhaven: School of Mages  (12 packs/box)
# 50/50 split: etched JP STA RM vs etched EN STA RM; common uncommon slot shared
# ---------------------------------------------------------------------------
def model_stx_collector_box() -> ProductModel:
    sc = "stx"
    q_sta_u  = _q("set:sta", "rarity:uncommon", "game:paper", "lang:en")
    q_sta_rm = _q("set:sta", "(rarity:rare or rarity:mythic)", "game:paper", "lang:en")
    q_ext_cmd = _q(f"set:{sc}", "rarity:rare", "is:extendedart", "game:paper", "lang:en")
    q_ext_bl  = _q(f"set:{sc}", "(rarity:rare or rarity:mythic)", "is:borderless", "game:paper", "lang:en")
    q_less    = _q(f"set:{sc}", "rarity:uncommon", "type:Lesson", "game:paper", "lang:en")
    q_alt_r   = _q(f"set:{sc}", "rarity:rare",   "is:extendedart", "game:paper", "lang:en")
    q_alt_m   = _q(f"set:{sc}", "rarity:mythic", "is:extendedart", "game:paper", "lang:en")
    p_r, p_m = _old(sc, 2 / 159, 1 / 159)
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 5),
        _fu(sc, 2),
        _frm(sc, 2 / 159, 1 / 159, old=True),
        Slot("Foil Lesson Uncommon", [(1.0, _qp("stx_less", q_less))], strict_probs=True),
        _treat(sc, 1 / 75, 1 / 150, "is:extendedart", foil=False, old=True, lbl="Extended Commander NF", tag="stx_ext_cmd"),
        Slot("Extended/Borderless Core NF",
             [(1.0, _qp("stx_ext_bl", q_ext_bl, f=False))], strict_probs=True),
        Slot("Foil STA Uncommon", [(1.0, _qp("stx_fsta_u", q_sta_u))], strict_probs=True),
        Slot("Foil Alt Art Wild",
             [(p_r, _qp("stx_alt_r", q_alt_r)), (p_m, _qp("stx_alt_m", q_alt_m))],
             strict_probs=True, renormalize=True),
        Slot("Etched STA (50% EN / 50% JP) — R/M",
             [(1.0, _qp("stx_eth_sta_rm", q_sta_rm, f=False))], strict_probs=True),
        Slot("Etched STA Uncommon (50% EN / 50% JP)",
             [(1.0, _qp("stx_eth_sta_u", q_sta_u, f=False))], strict_probs=True),
    ])


# ---------------------------------------------------------------------------
# TDM — Tarkir: Dragonstorm  (12 packs/box)
# Variant: 16.7 % NF basic / 83.3 % foil basic
# ---------------------------------------------------------------------------
def model_tdm_collector_box() -> ProductModel:
    sc = "tdm"
    q_drac_cu = _q(f"set:{sc}", "(rarity:common or rarity:uncommon)", "is:showcase", "game:paper", "lang:en")
    q_cmd     = _q(f"set:{sc}", "rarity:rare", "is:extendedart", "game:paper", "lang:en")
    q_basic   = _q(f"set:{sc}", "type:basic", "game:paper")
    basic_var = Slot(
        "Basic Land (16.7% NF / 83.3% foil)",
        [(0.167, _qp("tdm_basic_nf", q_basic, f=False)),
         (0.833, _qp("tdm_basic_f",  q_basic))],
        strict_probs=True,
    )
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 4),
        _fu(sc, 3),
        Slot("Draconic C/U NF",   [(1.0, _qp("tdm_drac_nf", q_drac_cu, f=False))], strict_probs=True),
        Slot("Foil Draconic C/U", [(1.0, _qp("tdm_fdrac",   q_drac_cu))],          strict_probs=True),
        basic_var,
        _frm(sc, 1 / 70, 1 / 140),
        Slot("Commander R/M NF", [(1.0, _qp("tdm_cmd", q_cmd, f=False))], strict_probs=True),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=False, n=2.0, lbl="2x BoosterFun R/M NF"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True,       lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# TLA — Avatar: The Last Airbender  (12 packs/box)
# Source material: 75 % NF / 25 % foil
# TLE = The Last Airbender Extras bonus set
# ---------------------------------------------------------------------------
def model_tla_collector_box() -> ProductModel:
    sc = "tla"
    tlesc = "tle"
    q_src = _q(f"set:{sc}", "(is:showcase or is:extendedart)", "(rarity:rare or rarity:mythic)", "game:paper", "lang:en")
    q_bf  = _q(f"set:{sc}", "(is:showcase or is:extendedart or is:borderless)", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fc(sc, 3),
        _fu(sc, 3),
        Slot("2x Foil TLE Common",     [(2.0, _qp("tle_fc", _q(f"set:{tlesc}", "rarity:common",   "game:paper", "lang:en")))], strict_probs=False),
        Slot("Foil TLE Scene Uncommon", [(1.0, _qp("tle_fsc_u", _q(f"set:{tlesc}", "rarity:uncommon", "game:paper", "lang:en")))], strict_probs=True),
        _fl(sc),
        _frm(sc, 1 / 70, 1 / 140),
        _bonus_rm(tlesc, 2 / 163, 1 / 163, foil=True, lbl="Foil TLE R/M"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=False, lbl="BoosterFun R/M NF"),
        Slot("Source Material (75% NF / 25% foil)",
             [(0.75, _qp("tla_src_nf", q_src, f=False)),
              (0.25, _qp("tla_src_f",  q_src))],
             strict_probs=True),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True, lbl="Foil BoosterFun R/M"),
    ])


# ---------------------------------------------------------------------------
# WOE — Wilds of Eldraine  (12 packs/box)
# Variant: 89.5 % extended_art NF / 10.5 % foil_extended_art
# Enchanting Tales = set:wot
# ---------------------------------------------------------------------------
def model_woe_collector_box() -> ProductModel:
    sc = "woe"
    wotsc = "wot"
    p_ext_r, p_ext_m = _rp(sc, 1 / 73, 1 / 146)
    q_ext_r = _q(f"set:{sc}", "rarity:rare",   "is:extendedart", "game:paper", "lang:en")
    q_ext_m = _q(f"set:{sc}", "rarity:mythic", "is:extendedart", "game:paper", "lang:en")
    ext_var = Slot(
        "Extended Art R/M (89.5% NF / 10.5% foil)",
        [(0.895 * p_ext_r, _qp("woe_ext_nf_r", q_ext_r, f=False)),
         (0.895 * p_ext_m, _qp("woe_ext_nf_m", q_ext_m, f=False)),
         (0.105 * p_ext_r, _qp("woe_ext_f_r",  q_ext_r)),
         (0.105 * p_ext_m, _qp("woe_ext_f_m",  q_ext_m))],
        strict_probs=True, renormalize=True,
    )
    q_wot_u  = _q(f"set:{wotsc}", "rarity:uncommon", "game:paper", "lang:en")
    return ProductModel(set_code=sc, packs_per_box=12, slots=[
        _fal(sc),
        _fc(sc, 4),
        _fu(sc, 4),
        Slot("Enchanting Tales Uncommon NF",  [(1.0, _qp("wot_u_nf",  q_wot_u, f=False))], strict_probs=True),
        Slot("Foil Enchanting Tales Uncommon",[(1.0, _qp("wot_u_f",   q_wot_u))],          strict_probs=True),
        _frm(sc, 1 / 70, 1 / 140),
        ext_var,
        _treat(sc, 1 / 24, 1 / 48, "is:showcase",    foil=False, lbl="Showcase R/M NF",           tag="woe_sc"),
        _bonus_rm(wotsc, 2 / 75, 1 / 75, lbl="Enchanting Tales R/M NF"),
        _bf_slot(sc, "(is:showcase or is:extendedart or is:borderless)", foil=True, lbl="Alt Frame Foil R/M"),
    ])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

COLLECTOR_MODEL_REGISTRY: dict[tuple[str, str], Callable[[], ProductModel]] = {
    ("2X2", "collector_box"): model_2x2_collector_box,
    ("ACR", "collector_box"): model_acr_collector_box,
    ("BLB", "collector_box"): model_blb_collector_box,
    ("BRO", "collector_box"): model_bro_collector_box,
    ("CMM", "collector_box"): model_cmm_collector_box,
    ("DFT", "collector_box"): model_dft_collector_box,
    ("DSK", "collector_box"): model_dsk_collector_box,
    ("ECL", "collector_box"): model_ecl_collector_box,
    ("EOE", "collector_box"): model_eoe_collector_box,
    ("FDN", "collector_box"): model_fdn_collector_box,
    ("FIN", "collector_box"): model_fin_collector_box,
    ("INR", "collector_box"): model_inr_collector_box,
    ("LCI", "collector_box"): model_lci_collector_box,
    ("LTR", "collector_box"): model_ltr_collector_box,
    ("MH2", "collector_box"): model_mh2_collector_box,
    ("MH3", "collector_box"): model_mh3_collector_box,
    ("MKM", "collector_box"): model_mkm_collector_box,
    ("MOM", "collector_box"): model_mom_collector_box,
    ("ONE", "collector_box"): model_one_collector_box,
    ("OTJ", "collector_box"): model_otj_collector_box,
    ("RVR", "collector_box"): model_rvr_collector_box,
    ("SPM", "collector_box"): model_spm_collector_box,
    ("STX", "collector_box"): model_stx_collector_box,
    ("TDM", "collector_box"): model_tdm_collector_box,
    ("TLA", "collector_box"): model_tla_collector_box,
    ("WOE", "collector_box"): model_woe_collector_box,
}
