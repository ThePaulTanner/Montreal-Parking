"""Turn the city's French sign text (DESCRIPTION_RPA) into a Rule.

The text is a loose grammar:  marker  [limit]  [time ranges]  [weekdays]  [date range]  [EXCEPTE ...]
    \\P 8h30-11h30 MERCREDI 1 AVRIL AU 1 DEC      no parking (street cleaning)
    \\P 9h-21h LUN A VEN EXCEPTE S3R              residents' permit only
    P 2h 8h - 22h                                  2 hour limit
    \\A 15h30-18h30 LUN. AU VEN.                   no stopping at rush hour
Only ~1,500 distinct strings exist, so each is parsed once and cached by the caller.
`partial` marks a rule where some words were not understood or a part was approximated;
wholly unrecognised text becomes an UNKNOWN rule and the app shows the raw sign text.
"""
import re
import unicodedata

from .rules import (ALLOWED, MODIFIER, NO_PARKING, NO_STOPPING, NOT_APPLICABLE, PAID,
                    UNKNOWN, Rule)

DAYS = {"LUN": 0, "MAR": 1, "MER": 2, "JEU": 3, "VEN": 4, "SAM": 5, "DIM": 6}
MONTHS = {"JAN": 1, "FEV": 2, "MARS": 3, "AVR": 4, "MAI": 5, "JUIN": 6, "JUIL": 7,
          "AOUT": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_LAST_DAY = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

_MONTH_RE = r"(JAN\w*|FEV\w*|MARS|AVR\w*|MAI|JUIN|JUIL\w*|AOUT|SEP\w*|OCT\w*|NOV\w*|DEC\w*)"
# "1 AVRIL AU 1 DEC", "15 MARS AU 15 NOVEMBRE", "SEPT A JUIN" (day numbers optional)
_DATE_RANGE = re.compile(rf"(?:(\d{{1,2}})\s*)?{_MONTH_RE}\s*(?:AU|A|-)\s*(?:(\d{{1,2}})\s*)?{_MONTH_RE}")
_DAY = re.compile(r"\b(LUNDI|LUN|MARDI|MAR|MERCREDI|MER|JEUDI|JEU|VENDREDI|VEN|SAMEDI|SAM|DIMANCHE|DIM)\b")
# 8h30-11h30, 9-23h, 07h-09 (an H on at least one side)
_TIME_RANGE = re.compile(
    r"(\d{1,2})\s*(?:(H)(\d{2})?)?\s*(?:-|AU|A)\s*(\d{1,2})\s*(?:(H)(\d{2})?)?")
_LIMIT = re.compile(r"^\s*(?:MAX\s*)?(\d+)\s*(MIN|MINUTES?|H|HEURES?)\b")
_LONE_TIME = re.compile(r"^\d{1,2}\s*H?\s*\d{0,2}\s*$")

_EXCEPT_SPLIT = re.compile(r"\b(EXCEPTE|SAUF)\b")
_EXC_WORDS = [
    ("permit", re.compile(r"\bS\d+R\b|PERMIS|VIGNETTE|DETENTEUR|SECTEUR|RESIDENT")),
    ("handicap", re.compile(r"HANDICAP|DEBARCADERE|PMR")),
    ("ev", re.compile(r"ELECTRIQUE|RECHARGE")),
    ("taxi", re.compile(r"TAXI")),
    ("moto", re.compile(r"MOTO")),
    ("delivery", re.compile(r"LIVR")),
]
_OTHER_VEHICLE = re.compile(
    r"\bAUX\s+(CAMION|AUTOBUS|TAXI|MOTO|VEHICULE|BUS|ECOLE)|VEHICULES? DE PLUS|REMORQUE|CARAVANE")
_PAID_PREFIX = re.compile(r"^(HORAIRE DE )?(STATIONNEMENT TARIFE|PARCOMETRE|PARCOJOUR)")

# words that describe the sign but add no schedule rule
_FILLER = {
    "EN", "TOUT", "TEMPS", "DEUX", "COTES", "COTE", "ET", "OU", "DE", "DES", "DU", "LA", "LE", "LES",
    "AU", "AUX", "A", "D", "SEULEMENT", "PICTO", "PICTOGRAMME", "VOITURE", "SECTEUR", "SECTEURS",
    "XX", "PANONCEAU", "AUTOCOL", "MAX", "TOUS", "JOURS", "H", "ANGLE", "CLIGNOTANT", "FIXE", "FLECHE",
    "RESERVE", "RESERVES", "ECOLE", "GARDERIE", "HOTEL", "POMPIER", "POMPIERS", "URGENCE", "POLICE",
    "SERVICE", "VEHICULE", "VEHICULES", "AUTOBUS", "ENTRETIEN", "1", "2", "NON", "SOUS", "PAR", "SUR",
}


def _normalize(text):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper()
    t = t.replace("\\\\", "\\").replace("@", " A ").replace("'", " ")
    for typo, fix in (("AVIL", "AVRIL"), ("LUNDRI", "LUNDI"), ("VEMDREDI", "VENDREDI"),
                      ("EXEPTE", "EXCEPTE"), ("/P", "\\P"), ("/A ", "\\A ")):
        t = t.replace(typo, fix)
    t = re.sub(r"EXCEPTE(?=[A-Z])", "EXCEPTE ", t)
    t = re.sub(r"(\d)ER\b", r"\1", t)
    t = re.sub(r"[()]", " ", t).replace(".", " ").replace(",", " ")
    return re.sub(r"\s+", " ", t).strip()


def _month(token):
    for k, v in MONTHS.items():
        if token.startswith(k):
            return v
    return None


def _minutes(h, m):
    return int(h) * 60 + (int(m) if m else 0)


def _parse_days(t):
    """Weekdays named in `t` ('LUN A VEN' ranges, 'LUN ET JEU' lists) and `t` with them removed."""
    found = [(m.start(), m.end(), DAYS[m.group(1)[:3]]) for m in _DAY.finditer(t)]
    days, i = [], 0
    while i < len(found):
        s, e, d = found[i]
        if i + 1 < len(found):
            between = t[e:found[i + 1][0]].strip()
            if between in ("A", "AU", "-"):
                d2 = found[i + 1][2]
                days.extend(range(d, d2 + 1) if d <= d2 else list(range(d, 7)) + list(range(0, d2 + 1)))
                i += 2
                continue
        days.append(d)
        i += 1
    return tuple(sorted(set(days))), _DAY.sub(" ", t)


def _time_windows(body):
    windows = []

    def take(m):
        if not (m.group(2) or m.group(5)):  # no H anywhere: not a time range
            return m.group(0)
        windows.append((_minutes(m.group(1), m.group(3)), _minutes(m.group(4), m.group(6))))
        return " "

    return windows, _TIME_RANGE.sub(take, body)


def _exceptions(tail):
    return tuple(sorted(name for name, rx in _EXC_WORDS if rx.search(tail)))


def parse_sign(text):
    """Parse one DESCRIPTION_RPA string into a Rule."""
    return parse_sign_debug(text)[0]


def parse_sign_debug(text):
    """(Rule, leftover words). Leftover is what the parser did not account for."""
    raw = text
    t = _normalize(text)
    if not t:
        return Rule(UNKNOWN, raw), []

    if t.startswith("PANONCEAU") or t.startswith("AUTOCOL"):
        return _modifier(raw, t)

    # Spelled-out variants of the \P / \A / P markers
    if t.startswith("ARRET INTERDIT"):
        t = "\\A " + t[len("ARRET INTERDIT"):]
    elif t.startswith("STATIONNEMENT INTERDIT"):
        t = "\\P " + t[len("STATIONNEMENT INTERDIT"):]
    elif re.match(r"^A \d", t):
        t = "\\A " + t[2:]
    elif re.match(r"^\d+ (MIN|H)\b", t):  # "60 MIN - 9H A 16H" has no marker; it is a stay limit
        t = "P " + t

    paid = _PAID_PREFIX.match(t)
    if paid:
        kind, body = PAID, t[paid.end():]
    elif t.startswith("\\P"):
        kind, body = NO_PARKING, t[2:]
    elif t.startswith("\\A"):
        kind, body = NO_STOPPING, t[2:]
    elif re.match(r"^P\b", t):
        kind, body = ALLOWED, t[1:]
    else:
        return Rule(UNKNOWN, raw, partial=True), [t]

    # maintenance / "flexible" dates are announced on the day, so they cannot be scheduled
    if "FLEXIBLE" in body or "ENTRETIEN" in body:
        return Rule(UNKNOWN, raw), []
    if _OTHER_VEHICLE.search(body):
        return Rule(NOT_APPLICABLE, raw), []

    exceptions = ()
    parts = _EXCEPT_SPLIT.split(body, maxsplit=1)
    if len(parts) == 3:
        body, exceptions = parts[0], _exceptions(parts[2])
        if not exceptions:
            exceptions = ("other",)

    if re.search(r"\bRESERVEE?S?\b", body) and kind != PAID:
        kind, exceptions = NO_PARKING, tuple(sorted(set(exceptions) | {"other"}))

    limit = None
    if kind == ALLOWED:
        m = _LIMIT.match(body)
        if m:
            rest = body[m.end():].lstrip(" -")
            if not _LONE_TIME.match(rest):  # else it was the start of a range like "9h-17h"
                n = int(m.group(1))
                limit = n if m.group(2).startswith("MIN") else n * 60
                body = rest

    date_from = date_to = None
    m = _DATE_RANGE.search(body)
    if m:
        m1, m2 = _month(m.group(2)), _month(m.group(4))
        date_from = (m1, int(m.group(1)) if m.group(1) else 1)
        date_to = (m2, int(m.group(3)) if m.group(3) else _LAST_DAY[m2])
        body = body[:m.start()] + " " + body[m.end():]

    approximate = False
    if "JOURS ECOLE" in body or "JOURS DE CLASSE" in body:
        approximate = True  # school days: approximated as Sept-June weekdays
        body = body.replace("JOURS ECOLE", " ").replace("JOURS DE CLASSE", " ")
        date_from, date_to = date_from or (9, 1), date_to or (6, 30)
        days = (0, 1, 2, 3, 4)
    else:
        days = ()
    parsed_days, body = _parse_days(body)
    days = parsed_days or days

    windows, body = _time_windows(body)

    leftover = [w for w in re.findall(r"[A-Z0-9$]+", body) if w not in _FILLER]
    return Rule(kind, raw, limit_min=limit, windows=tuple(windows), days=days,
                date_from=date_from, date_to=date_to, exceptions=tuple(sorted(exceptions)),
                partial=bool(leftover) or approximate), leftover


def _modifier(raw, t):
    """A panonceau under a panel: adds exceptions and/or a date range to the panel above."""
    ex = _exceptions(t)
    date_from = date_to = None
    m = _DATE_RANGE.search(t)
    if m:
        m1, m2 = _month(m.group(2)), _month(m.group(4))
        date_from = (m1, int(m.group(1)) if m.group(1) else 1)
        date_to = (m2, int(m.group(3)) if m.group(3) else _LAST_DAY[m2])
    understood = bool(ex or m)
    return (Rule(MODIFIER, raw, exceptions=ex, date_from=date_from, date_to=date_to, partial=not understood),
            [] if understood else [t])
