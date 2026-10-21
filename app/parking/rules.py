"""Parking rule model and "what applies at this moment" evaluation.

A Rule is one sign panel (or one Agence regulation) reduced to: what it does (kind), when
it is in force (windows, days, date range) and who it exempts. `evaluate` combines all the
rules at one spot for a given local time into a single Status.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

# Kinds
ALLOWED = "P"        # parking permitted (optionally for a limited time)
NO_PARKING = "NP"    # \P
NO_STOPPING = "NS"   # \A
PAID = "PAID"        # metered
MODIFIER = "MOD"     # panonceau: changes the panel above
NOT_APPLICABLE = "NA"  # applies only to other vehicles (trucks, buses...)
UNKNOWN = "UNK"      # could not be understood; the raw text is shown instead

# Statuses, lowest to highest priority. The most restrictive active rule wins.
FREE, UNCERTAIN, LIMITED, PAID_S, PERMIT, RESERVED, NO_PARK, NO_STOP = (
    "free", "unknown", "limited", "paid", "permit", "reserved", "no_parking", "no_stopping")
PRIORITY = {s: i for i, s in enumerate(
    [FREE, UNCERTAIN, LIMITED, PAID_S, PERMIT, RESERVED, NO_PARK, NO_STOP])}


@dataclass(frozen=True)
class Rule:
    kind: str
    text: str = ""
    limit_min: int | None = None            # maximum stay, minutes
    windows: tuple = ()                     # ((start_min, end_min), ...) minutes after midnight; empty = all day
    days: tuple = ()                        # weekdays in force, 0=Mon; empty = every day
    date_from: tuple | None = None          # (month, day) inclusive
    date_to: tuple | None = None
    exceptions: tuple = ()                  # sorted: permit, handicap, ev, taxi, moto, delivery, other
    tariff_cents: int | None = None         # PAID: cents per hour
    max_cents: int | None = None            # PAID: daily cap in cents
    partial: bool = False                   # some words in the text were not understood


@dataclass(frozen=True)
class Status:
    status: str
    detail: str = ""                         # short human text, e.g. "2 h max" or "$4.25/h"
    limit_min: int | None = None
    cents: int | None = None
    rule_text: str = ""
    exceptions: tuple = ()


def _in_date_range(rule, dt):
    if rule.date_from is None or rule.date_to is None:
        return True
    cur = (dt.month, dt.day)
    if rule.date_from <= rule.date_to:
        return rule.date_from <= cur <= rule.date_to
    return cur >= rule.date_from or cur <= rule.date_to  # wraps the new year


def _day_ok(rule, weekday):
    return not rule.days or weekday in rule.days


def is_active(rule, dt):
    """True if `rule` is in force at the local datetime `dt`."""
    if not _in_date_range(rule, dt):
        return False
    if not rule.windows:
        return _day_ok(rule, dt.weekday())
    t = dt.hour * 60 + dt.minute
    for start, end in rule.windows:
        if start == end:  # a 24 h window
            if _day_ok(rule, dt.weekday()):
                return True
        elif start < end:
            if start <= t < end and _day_ok(rule, dt.weekday()):
                return True
        else:  # crosses midnight; the day belongs to the evening part
            if t >= start and _day_ok(rule, dt.weekday()):
                return True
            if t < end and _day_ok(rule, (dt - timedelta(days=1)).weekday()):
                return True
    return False


def _fmt_limit(minutes):
    if minutes % 60 == 0:
        return f"{minutes // 60} h max"
    return f"{minutes} min max"


def _rule_status(rule):
    ex = rule.exceptions
    if rule.kind == NO_STOPPING:
        return Status(NO_STOP, "No stopping", rule_text=rule.text, exceptions=ex)
    if rule.kind == NO_PARKING:
        if "permit" in ex:
            return Status(PERMIT, "Permit holders only", rule_text=rule.text, exceptions=ex)
        reserved = [e for e in ex if e != "other"]
        if reserved:
            return Status(RESERVED, "Reserved: " + ", ".join(reserved), rule_text=rule.text, exceptions=ex)
        if ex:
            return Status(RESERVED, "Reserved", rule_text=rule.text, exceptions=ex)
        return Status(NO_PARK, "No parking", rule_text=rule.text)
    if rule.kind == PAID:
        d = f"${rule.tariff_cents / 100:.2f}/h" if rule.tariff_cents is not None else "Paid"
        if rule.limit_min:
            d += f", {_fmt_limit(rule.limit_min)}"
        return Status(PAID_S, d, limit_min=rule.limit_min, cents=rule.tariff_cents, rule_text=rule.text)
    if rule.kind == ALLOWED:
        if rule.limit_min:
            return Status(LIMITED, _fmt_limit(rule.limit_min), limit_min=rule.limit_min, rule_text=rule.text)
        return Status(FREE, "Parking allowed", rule_text=rule.text)
    return Status(UNCERTAIN, "Check the sign", rule_text=rule.text)


def evaluate(rules, dt):
    """Most restrictive status among the rules in force at `dt` (free if none apply)."""
    best = Status(FREE, "No restriction")
    saw_unknown = None
    for r in rules:
        if r.kind in (MODIFIER, NOT_APPLICABLE):
            continue
        if r.kind == UNKNOWN:
            saw_unknown = r
            continue
        if not is_active(r, dt):
            continue
        s = _rule_status(r)
        if PRIORITY[s.status] > PRIORITY[best.status]:
            best = s
        elif s.status == best.status == PAID_S and (s.cents or 0) > (best.cents or 0):
            best = s
    if best.status == FREE and saw_unknown is not None:
        return Status(UNCERTAIN, "Check the sign", rule_text=saw_unknown.text)
    return best


def next_change(rules, dt, horizon_hours=72, step_min=15):
    """(datetime, Status) of the next moment the status differs from now, or (None, None).

    Scans in coarse steps, then walks the last step minute by minute so the time is exact.
    """
    now = evaluate(rules, dt)
    key = (now.status, now.detail)
    t = dt.replace(second=0, microsecond=0)
    for _ in range(horizon_hours * 60 // step_min):
        nxt = t + timedelta(minutes=step_min)
        s = evaluate(rules, nxt)
        if (s.status, s.detail) != key:
            for m in range(1, step_min + 1):
                u = t + timedelta(minutes=m)
                su = evaluate(rules, u)
                if (su.status, su.detail) != key:
                    return u, su
        t = nxt
    return None, None
