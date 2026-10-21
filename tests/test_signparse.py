import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from parking.rules import (ALLOWED, FREE, LIMITED, MODIFIER, NO_PARK, NO_PARKING, NO_STOP, NO_STOPPING,
                           NOT_APPLICABLE, PERMIT, RESERVED, UNKNOWN, evaluate, next_change)
from parking.signparse import parse_sign

# 2026-10-21 is a Wednesday
WED_10 = datetime(2026, 10, 21, 10, 0)
WED_22 = datetime(2026, 10, 21, 22, 0)


def st(text, dt):
    return evaluate([parse_sign(text)], dt).status


def test_all_day_no_parking_and_no_stopping():
    assert st(r"\P EN TOUT TEMPS", WED_10) == NO_PARK
    assert st(r"\A EN TOUT TEMPS", WED_10) == NO_STOP
    assert st(r"\P DEUX COTES", WED_10) == NO_PARK


def test_street_cleaning_window_and_season():
    t = r"\P 8h30-11h30 MERCREDI 1 AVRIL AU 1 DEC"
    assert st(t, datetime(2026, 10, 21, 9, 0)) == NO_PARK       # Wednesday morning in season
    assert st(t, datetime(2026, 10, 21, 12, 0)) == FREE         # after the window
    assert st(t, datetime(2026, 10, 22, 9, 0)) == FREE          # Thursday
    assert st(t, datetime(2026, 1, 21, 9, 0)) == FREE           # a Wednesday in January: out of season


def test_year_wrapping_season():
    t = r"\P 8h-12h JEUDI 1ER DEC - 1ER MARS"
    assert st(t, datetime(2026, 1, 15, 9, 0)) == NO_PARK
    assert st(t, datetime(2026, 7, 16, 9, 0)) == FREE


def test_permit_exception():
    t = r"\P 9h-21h LUN A VEN EXCEPTE S3R"
    assert st(t, WED_10) == PERMIT
    assert st(t, WED_22) == FREE
    assert st(t, datetime(2026, 10, 24, 10, 0)) == FREE         # Saturday


def test_reserved_for_disabled_and_ev():
    assert st(r"\P EXCEPTE HANDICAPES (PICTO)", WED_10) == RESERVED
    assert st(r"\P EXCEPTE PICTOGRAMME VOITURE ELECTRIQUE - EN RECHARGE", WED_10) == RESERVED


def test_stay_limit_followed_by_time_range():
    r = parse_sign("P 02h 09h-18h")                              # old bug: read as 2:09
    assert r.kind == ALLOWED and r.limit_min == 120 and r.windows == ((540, 1080),)
    r = parse_sign("P 60 min 09h-16h LUN. AU VEN.")
    assert r.limit_min == 60 and r.days == (0, 1, 2, 3, 4)
    assert st("P 2h 8h - 22h", WED_10) == LIMITED
    assert st("P 2h 8h - 22h", WED_22) == FREE
    assert parse_sign("P 15 min").limit_min == 15


def test_permitted_with_hours_is_not_a_limit():
    r = parse_sign("P 9h-17h")
    assert r.limit_min is None and r.windows == ((540, 1020),)


def test_two_windows_on_one_sign():
    r = parse_sign(r"\A 07h-09h  16h-18h LUN. AU VEN.")
    assert r.windows == ((420, 540), (960, 1080))


def test_midnight_crossing_window_uses_start_day():
    t = r"\P 19h-7h LUN"
    assert st(t, datetime(2026, 10, 19, 20, 0)) == NO_PARK      # Monday evening
    assert st(t, datetime(2026, 10, 20, 3, 0)) == NO_PARK       # Tuesday 3 am, still Monday's window
    assert st(t, datetime(2026, 10, 19, 3, 0)) == FREE          # Monday 3 am belongs to Sunday


def test_other_vehicles_do_not_apply():
    assert parse_sign(r"\P AUX CAMIONS").kind == NOT_APPLICABLE
    assert st(r"\P AUX CAMIONS", WED_10) == FREE


def test_modifiers_and_unknowns():
    assert parse_sign("PANONCEAU EXCEPTÉ DETENTEURS DE PERMIS SECTEUR XX").kind == MODIFIER
    assert parse_sign(r"\P ENTRETIEN (ORANGE) 07h-19h ou 19h-07h (flexible)").kind == UNKNOWN
    assert st("n'importe quoi", WED_10) == "unknown"


def test_month_only_range_and_school_days():
    r = parse_sign("P 15 min 07h-09h ET 15h-18h LUN A VEN SEPT. A JUIN")
    assert r.date_from == (9, 1) and r.date_to == (6, 30) and r.limit_min == 15


def test_next_change():
    rules = [parse_sign(r"\P 9h-21h LUN A VEN EXCEPTE S3R")]
    when, new = next_change(rules, WED_22.replace(hour=20))
    assert new.status == FREE and when.hour == 21


def test_next_change_is_exact_to_the_minute():
    rules = [parse_sign(r"\P 9h30-10h30 JEUDI 1 AVRIL AU 1 DEC")]
    when, new = next_change(rules, datetime(2026, 10, 21, 14, 2))  # Wednesday afternoon
    assert (when.day, when.hour, when.minute) == (22, 9, 30) and new.status == NO_PARK
    when, new = next_change(rules, datetime(2026, 10, 22, 9, 31))
    assert (when.hour, when.minute) == (10, 30) and new.status == FREE
