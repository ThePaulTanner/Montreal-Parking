"""Load the Agence de mobilité durable paid-parking tables into Rules per bay.

Join chain (all CSV, Windows-1252):
  Places.sNoPlace -> EmplacementReglementation.sNoEmplacement -> sCodeAutocollant
  -> Reglementations.Name (Type, DateDebut/DateFin as DDMM, maxHeures)
  -> ReglementationPeriode.sCode/noPeriode -> Periodes.nID (start/end time + 7 weekday flags)

Regulation types (the Agence does not document them; inferred from their descriptions):
  U paid, P/Q maximum stay, I/A no parking, R no stopping, G free parking, H disabled only,
  B electric vehicle only, D/M daily rate, Z/F notices (ignored).
"""
import csv
import os
from collections import defaultdict

from .rules import ALLOWED, NO_PARKING, NO_STOPPING, PAID, Rule

DAY_COLS = ["bLun", "bMar", "bMer", "bJeu", "bVen", "bSam", "bDim"]


def _read(path, name):
    with open(os.path.join(path, name + ".csv"), encoding="cp1252", newline="") as f:
        return list(csv.DictReader(f))


def _ddmm(s):
    s = (s or "").zfill(4)
    return (int(s[2:]), int(s[:2]))  # (month, day)


def _hhmm(s):
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def load(path):
    """Returns a list of dicts: {id, lon, lat, street, key, rules} one per bay.

    `key` identifies bays that share exactly the same rules, so callers can intern them.
    """
    places = _read(path, "Places")
    regs = {r["Name"]: r for r in _read(path, "Reglementations")}
    periods = {}
    for p in _read(path, "Periodes"):
        start, end = _hhmm(p["dtHeureDebut"]), _hhmm(p["dtHeureFin"])
        if end == 0 and start != 0:
            end = 1440  # "until midnight"
        periods[p["nID"]] = (start, end, tuple(i for i, c in enumerate(DAY_COLS) if p[c] == "1"))
    code_periods = defaultdict(list)
    for rp in _read(path, "ReglementationPeriode"):
        code_periods[rp["sCode"]].append(rp["noPeriode"])
    place_codes = defaultdict(list)
    for er in _read(path, "EmplacementReglementation"):
        place_codes[er["sNoEmplacement"]].append(er["sCodeAutocollant"])

    cache = {}
    out = []
    for p in places:
        codes = tuple(sorted(set(place_codes.get(p["sNoPlace"], []))))
        tariff = int(p["nTarifHoraire"]) if p["nTarifHoraire"] else None
        cap = int(p["nTarifMax"]) if p["nTarifMax"] else None
        disabled = p["sGenre"] == "HANDICAPÉ"
        key = (codes, tariff, cap, disabled)
        if key not in cache:
            cache[key] = _rules_for(codes, regs, code_periods, periods, tariff, cap, disabled)
        lon = p["nPositionCentreLongitude"] or p["nLongitude"]
        lat = p["nPositionCentreLatitude"] or p["nLatitude"]
        out.append({"id": p["sNoPlace"], "lon": float(lon), "lat": float(lat),
                    "street": p["sNomRue"], "key": key, "rules": cache[key]})
    return out


def _rules_for(codes, regs, code_periods, periods, tariff, cap, disabled):
    rules = []
    if disabled:
        rules.append(Rule(NO_PARKING, "Disabled parking", exceptions=("handicap",)))
    for code in codes:
        reg = regs[code]
        typ = reg["Type"]
        if typ in ("Z", "F", "D", "M"):
            continue
        date_from, date_to = _ddmm(reg["DateDebut"]), _ddmm(reg["DateFin"])
        if date_from == (1, 1) and date_to == (12, 31):
            date_from = date_to = None
        max_h = int(reg["maxHeures"] or 0)
        limit = max_h * 60 if 0 < max_h < 100 else None  # 1000/1100 are sentinels, not hours
        label = f"{code} ({typ})"
        pers = [periods[n] for n in code_periods.get(code, []) if n in periods] or [(0, 0, ())]
        seen = set()
        for start, end, days in pers:
            if (start, end, days) in seen:
                continue
            seen.add((start, end, days))
            common = dict(text=label, windows=((start, end),) if (start, end) != (0, 0) else (),
                          days=days if len(days) < 7 else (), date_from=date_from, date_to=date_to)
            if typ == "U":
                rules.append(Rule(PAID, tariff_cents=tariff, max_cents=cap, limit_min=limit, **common))
            elif typ in ("P", "Q"):
                rules.append(Rule(ALLOWED, limit_min=limit, **common))
            elif typ in ("I", "A"):
                rules.append(Rule(NO_PARKING, **common))
            elif typ == "R":
                rules.append(Rule(NO_STOPPING, **common))
            elif typ == "G":
                rules.append(Rule(ALLOWED, **common))
            elif typ == "H":
                rules.append(Rule(NO_PARKING, exceptions=("handicap",), **common))
            elif typ == "B":
                rules.append(Rule(NO_PARKING, exceptions=("ev",), **common))
    return tuple(rules)
