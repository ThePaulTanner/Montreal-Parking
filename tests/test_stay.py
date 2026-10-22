import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from parking import agence  # noqa: F401  (imported to make sure the module loads)
from parking.rules import GO, GO_PAID, MAYBE, NO, PAID, Rule, stay_check
from parking.signparse import parse_sign
from parking.store import Store

WED_10 = datetime(2026, 10, 21, 10, 0)     # Wednesday
H = 60


def rules(*texts):
    return [parse_sign(t) for t in texts]


def test_no_signs_or_only_unrelated_ones_fit_any_stay():
    assert stay_check([], WED_10, 8 * H).result == GO
    assert stay_check(rules(r"\P 8h30-11h30 JEUDI 1 AVRIL AU 1 DEC"), WED_10, 2 * H).result == GO  # Thursday sign


def test_street_cleaning_tomorrow_blocks_an_overnight_stay_but_not_a_short_one():
    sweep = rules(r"\P 8h30-11h30 JEUDI 1 AVRIL AU 1 DEC")
    evening = datetime(2026, 10, 21, 20, 0)
    assert stay_check(sweep, evening, 4 * H).result == GO                  # ends at midnight
    f = stay_check(sweep, evening, 14 * H)                                  # runs into Thursday 08:30
    assert f.result == NO and (f.at.day, f.at.hour, f.at.minute) == (22, 8, 30) and f.status == "no_parking"


def test_a_stay_may_end_exactly_when_a_restriction_starts():
    sweep = rules(r"\P 8h30-11h30 JEUDI 1 AVRIL AU 1 DEC")
    assert stay_check(sweep, datetime(2026, 10, 22, 6, 30), 2 * H).result == GO   # 06:30 to 08:30
    assert stay_check(sweep, datetime(2026, 10, 22, 6, 30), 2 * H + 1).result == NO


def test_restriction_in_force_right_now_blocks():
    f = stay_check(rules(r"\A EN TOUT TEMPS"), WED_10, 30)
    assert f.result == NO and f.at == WED_10 and f.status == "no_stopping"
    assert stay_check(rules(r"\P 9h-21h LUN A VEN EXCEPTE S3R"), WED_10, 60).result == NO   # permit only


def test_time_limit_must_cover_the_whole_stay():
    two_hours = rules("P 2h 8h - 22h")
    assert stay_check(two_hours, WED_10, 2 * H).result == GO
    f = stay_check(two_hours, WED_10, 8 * H)
    assert f.result == NO and f.status == "limited"
    assert stay_check(two_hours, datetime(2026, 10, 21, 22, 30), 8 * H).result == GO   # limit is not in force overnight


def test_paid_parking_only_fits_when_you_accept_it():
    meter = [Rule(PAID, "m", tariff_cents=425, windows=((540, 1260),), days=(0, 1, 2, 3, 4), limit_min=2 * H)]
    assert stay_check(meter, WED_10, 2 * H).result == NO
    f = stay_check(meter, WED_10, 2 * H, accept_paid=True)
    assert f.result == GO_PAID and f.cost_cents == 850                      # 2 h at $4.25
    assert stay_check(meter, WED_10, 3 * H, accept_paid=True).result == NO   # the meter's own 2 h maximum
    evening = datetime(2026, 10, 21, 21, 30)
    assert stay_check(meter, evening, 8 * H).result == GO                    # meter is off after 21:00


def test_unreadable_signs_are_flagged_not_hidden():
    assert stay_check(rules(r"\P ENTRETIEN (ORANGE) 07h-19h ou 19h-07h (flexible)"), WED_10, 2 * H).result == MAYBE
    # but a clear restriction still wins over an unreadable one
    both = rules(r"\P ENTRETIEN (ORANGE) 07h-19h ou 19h-07h (flexible)", r"\A EN TOUT TEMPS")
    assert stay_check(both, WED_10, 2 * H).result == NO


def test_viewport_returns_only_curbs_that_fit():
    st = Store("unused")
    st.rulesets = [(), tuple(rules(r"\A EN TOUT TEMPS")), tuple(rules(r"\P ENTRETIEN (ORANGE) 07h-19h ou 19h-07h (flexible)"))]
    line = lambda k: (f"street {k}", "Droite", k, (-73.57, 45.52 + k * 1e-4, -73.569, 45.52 + k * 1e-4), k, 1, 0)
    st.portions = [line(0), line(1), line(2)]
    st._grid = {(int(-73.57 // 0.004), int(45.52 // 0.004)): [0, 1, 2]}
    got = st.viewport(-73.58, 45.51, -73.56, 45.53, WED_10, stay=120)
    assert {f["properties"]["i"]: f["properties"]["s"] for f in got["features"]} == {0: "go", 2: "maybe"}
    everything = st.viewport(-73.58, 45.51, -73.56, 45.53, WED_10)
    assert len(everything["features"]) == 3          # without a stay, every curb is returned with its own status
    info = st.describe(1, WED_10, stay=120)
    assert info["fit"]["result"] == "no" and info["fit"]["status"] == "no_stopping"
