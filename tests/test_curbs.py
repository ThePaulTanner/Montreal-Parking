"""Which stretch of a curb each sign governs, using the arrow codes (cases taken from real Lachine blocks)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pytest

from parking.curbs import cut_line, claim_intervals
from parking.signparse import parse_sign

EV = parse_sign(r"\P EXCEPTE PICTOGRAMME VOITURE ELECTRIQUE - EN RECHARGE")
NO_PARK = parse_sign(r"\P EN TOUT TEMPS")
SWEEP = parse_sign(r"\P 12h-16H LUNDI 15 MARS AU 15 NOVEMBRE")


def pieces(panels, length, bays=()):
    claims = [(a, b, r, 0) for a, b, r in claim_intervals(panels, length)] + list(bays)
    return [(round(a), round(b), len(rules), ns) for a, b, rules, ns, nb in cut_line(claims, length)]


def test_two_ev_poles_with_inward_arrows_reserve_only_the_stretch_between_them():
    # avenue 24e, Lachine: poles at 5% (arrow 3) and 11% (arrow 2) of a 213 m curb; the EV spaces are the ~13 m between
    L = 213.0
    got = claim_intervals([(0.05 * L, "3", EV), (0.11 * L, "2", EV)], L)
    assert [(round(a), round(b)) for a, b, _ in got] == [(11, 23), (11, 23)]
    # the rest of the curb is not covered: no sign data there, not "EV only"
    assert pieces([(0.05 * L, "3", EV), (0.11 * L, "2", EV)], L) == [(0, 11, 0, 0), (11, 23, 1, 2), (23, 213, 0, 0)]


def test_corner_zones_run_from_the_poles_to_the_ends_of_the_curb():
    # arrows pointing away from each other: 2 near the start covers the start, 3 near the end covers the end
    L = 94.0
    got = claim_intervals([(12.0, "2", NO_PARK), (86.0, "3", NO_PARK)], L)
    assert [(round(a), round(b)) for a, b, _ in got] == [(0, 12), (86, 94)]


def test_a_sign_with_no_arrow_governs_the_whole_short_curb_and_overlaps_corner_zones():
    L = 94.0
    panels = [(12.0, "2", NO_PARK), (62.0, "0", SWEEP), (86.0, "3", NO_PARK)]
    assert pieces(panels, L) == [(0, 12, 2, 2), (12, 86, 1, 1), (86, 94, 2, 2)]  # corners carry both rules


def test_a_sign_with_no_arrow_does_not_reach_further_than_100_m():
    got = claim_intervals([(70.0, "0", SWEEP)], 324.0)
    assert [(round(a), round(b)) for a, b, _ in got] == [(0, 170)]


def test_identical_signs_with_no_arrow_split_the_curb_at_their_midpoint():
    got = claim_intervals([(20.0, "0", SWEEP), (80.0, "0", SWEEP)], 120.0)
    assert [(round(a), round(b)) for a, b, _ in got] == [(0, 50), (50, 120)]


def test_signs_with_different_rules_do_not_pair_up():
    # an EV pole must not be closed by a no-parking pole's arrow
    got = claim_intervals([(20.0, "3", EV), (60.0, "2", NO_PARK)], 100.0)
    spans = {r.text: (round(a), round(b)) for a, b, r in got}
    assert spans[EV.text] == (20, 100) and spans[NO_PARK.text] == (0, 60)


def test_paid_bays_claim_seven_metres_each_and_neighbours_merge():
    bays = [(a - 3.5, a + 3.5, NO_PARK, 1) for a in (30.0, 37.0, 44.0)]   # three touching bays
    assert pieces([], 100.0, bays) == [(0, 26, 0, 0), (26, 48, 1, 0), (48, 100, 0, 0)]


def test_same_text_typed_differently_still_pairs():
    a = parse_sign(r"\P EXCEPTE HANDICAPES (PICTO)")
    b = parse_sign(r"\P  EXCEPTE   HANDICAPES (PICTO)")
    got = claim_intervals([(10.0, "3", a), (22.0, "2", b)], 100.0)
    assert [(round(x), round(y)) for x, y, _ in got] == [(10, 22), (10, 22)]
