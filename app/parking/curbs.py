"""Build curb segments: every street side cut into pieces that each carry the rules in force there.

Inputs
  signalisation_stationnement.csv   city signs, one row per panel, with the pole's X/Y (MTM zone 8, metres)
                                    and FLECHE_PAN, the arrow on the panel
  Agence CSV tables                 paid bays (lon/lat), see agence.py
  geobase_double.shp                city "Géobase double": one polyline per street side (MTM zone 8)

Method
  1. Parse each distinct sign text once, attach panonceaux to the panel above, keep each panel's arrow.
  2. Snap every pole and bay to the nearest curb line (<= 25 m) and note its distance along the line
     (validated by spikes/street_sides.py: 99% of installed poles are within 25 m of a curb line).
  3. Turn each panel into a claim on a stretch of its line, using its arrow (`claim_intervals`).
  4. Cut the line wherever a claim starts or ends. Each piece carries the rules of every claim over it;
     a piece nobody claims has no sign data. Neighbouring pieces with the same rules are merged.

Arrow codes (measured: short reserved bays such as EV and disabled spaces come as a pair of poles, and in
97% of pairs, on both sides of the street, the pole nearer the start of the line has 3 and the other 2):
  3  points toward the END of the line; the sign governs from its pole to its partner (same text, arrow 2)
  2  points toward the START of the line; the sign governs from its partner (arrow 3) to its pole
  0  and the rare codes (8, 22, 10, 11, 1): no direction; the sign governs the curb around it
"""
import csv
import os
import pickle
import time
from collections import defaultdict
from dataclasses import replace

import numpy as np
import shapefile
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import LineString, Point
from shapely.ops import substring

from . import agence
from .rules import MODIFIER, NOT_APPLICABLE
from .signparse import parse_sign

MAX_SNAP_M = 25.0
BAY_HALF_M = 3.5      # a paid bay is about 7 m long
DIRECTIONAL_REACH_M = 150.0   # an arrow sign with no partner runs this far (or to the end of the curb)
BIDIRECTIONAL_REACH_M = 100.0  # a sign with no arrow reaches this far each way (same-text signs repeat every ~56 m)
SIGN_CATS = {"STATIONNEMENT", "STAT-$"}  # STAT. HORS RUE is off-street


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _apply_modifiers(panels):
    """Fold panonceaux into the panel above them. `panels` is [(arrow, Rule)]; returns the real panels."""
    out = []
    for arrow, r in panels:
        if r.kind == MODIFIER:
            if out:
                prev_arrow, prev = out[-1]
                out[-1] = (prev_arrow, replace(
                    prev,
                    exceptions=tuple(sorted(set(prev.exceptions) | set(r.exceptions))),
                    date_from=prev.date_from or r.date_from,
                    date_to=prev.date_to or r.date_to))
            continue
        out.append((arrow, r))
    return out


def _same_sign(rule):
    """Two panels are the same restriction if they differ only in how the text was typed."""
    return replace(rule, text="")


def claim_intervals(panels, length):
    """The stretch of one curb line (metres from its start) each panel governs.

    `panels` is [(position, arrow, Rule)] for every panel snapped to the line; returns [(start, end, Rule)].
    """
    out = []
    for p, arrow, rule in panels:
        key = _same_sign(rule)
        same = [(q, a) for q, a, r in panels if _same_sign(r) == key]
        if arrow == "3":      # toward the end: runs to the partner pole (arrow 2), else on toward the corner
            ends = [q for q, a in same if a == "2" and q > p]
            a, b = p, (min(ends) if ends else min(length, p + DIRECTIONAL_REACH_M))
        elif arrow == "2":    # toward the start: runs back to the partner (arrow 3), else back to the corner
            starts = [q for q, a in same if a == "3" and q < p]
            a, b = (max(starts) if starts else max(0.0, p - DIRECTIONAL_REACH_M)), p
        else:                 # no direction: the curb around it, up to halfway to the next identical sign
            others = [q for q, a in same if a not in ("2", "3") and q != p]
            lo = max([(q + p) / 2 for q in others if q < p], default=max(0.0, p - BIDIRECTIONAL_REACH_M))
            hi = min([(q + p) / 2 for q in others if q > p], default=min(length, p + BIDIRECTIONAL_REACH_M))
            a, b = lo, hi
        a, b = max(0.0, a), min(length, b)
        if b - a < 1.0:       # never a zero-length claim
            a, b = max(0.0, a - 0.5), min(length, b + 0.5)
        out.append((a, b, rule))
    return out


def cut_line(claims, length):
    """Split a line at every claim boundary. `claims` is [(start, end, Rule, kind)] with kind 0=sign, 1=bay.
    Returns merged pieces [start, end, rules_tuple, n_signs, n_bays]; rules_tuple is empty where nothing claims."""
    cuts = sorted({0.0, length} | {c[0] for c in claims} | {c[1] for c in claims})
    pieces = []
    for x0, x1 in zip(cuts, cuts[1:]):
        if x1 - x0 < 0.3:
            continue
        mid = (x0 + x1) / 2
        cover = [c for c in claims if c[0] <= mid <= c[1]]
        rules = tuple(sorted({c[2] for c in cover}, key=repr))
        n_signs = sum(1 for c in cover if c[3] == 0)
        n_bays = len(cover) - n_signs
        if pieces and pieces[-1][2] == rules:
            pieces[-1][1] = x1
            pieces[-1][3], pieces[-1][4] = max(pieces[-1][3], n_signs), max(pieces[-1][4], n_bays)
        else:
            pieces.append([x0, x1, rules, n_signs, n_bays])
    return pieces


def load_poles(sign_csv):
    """{pole_id: (x, y, [(arrow, Rule), ...])} for installed on-street poles."""
    parse_cache = {}
    panels = defaultdict(list)
    xy = {}
    with open(sign_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["DESCRIPTION_REP"] != "Réel" or row["DESCRIPTION_CAT"] not in SIGN_CATS:
                continue
            if row["PAS_SUR_RUE"] == "1":
                continue
            text = row["DESCRIPTION_RPA"]
            if text not in parse_cache:
                parse_cache[text] = parse_sign(text)
            pid = row["POTEAU_ID_POT"]
            xy[pid] = (float(row["X"]), float(row["Y"]))
            panels[pid].append((int(row["POSITION_POP"] or 0), row["FLECHE_PAN"], parse_cache[text]))
    poles = {}
    for pid, items in panels.items():
        items.sort(key=lambda it: it[0])
        real = _apply_modifiers([(arrow, r) for _, arrow, r in items])
        poles[pid] = (*xy[pid], [(arrow, r) for arrow, r in real if r.kind != NOT_APPLICABLE])
    return poles


def build(raw_dir, geo_dir, out_path):
    t0 = time.time()
    log("parsing signs")
    poles = load_poles(os.path.join(raw_dir, "signalisation_stationnement.csv"))
    log(f"{len(poles)} poles")
    bays = agence.load(raw_dir)
    log(f"{len(bays)} paid bays")

    to_mtm = Transformer.from_crs(4326, 32188, always_xy=True)
    to_wgs = Transformer.from_crs(32188, 4326, always_xy=True)

    rulesets, ruleset_ids = [], {}

    def intern(rules):
        if rules not in ruleset_ids:
            ruleset_ids[rules] = len(rulesets)
            rulesets.append(rules)
        return ruleset_ids[rules]

    ax, ay, apan, akind = [], [], [], []
    for x, y, panels in poles.values():
        ax.append(x), ay.append(y), apan.append(panels), akind.append(0)
    if bays:
        bx, by = to_mtm.transform([b["lon"] for b in bays], [b["lat"] for b in bays])
        for x, y, b in zip(bx, by, bays):
            ax.append(x), ay.append(y), apan.append(b["rules"]), akind.append(1)

    log("reading street sides")
    sf = shapefile.Reader(os.path.join(geo_dir, "geobase_double.shp"), encoding="utf-8")
    lines, meta = [], []
    for sr in sf.iterShapeRecords():
        if len(sr.shape.points) < 2:
            continue
        lines.append(LineString(sr.shape.points))
        rec = sr.record.as_dict()
        name = " ".join(x for x in (rec["TYPE_F"], rec["NOM_VOIE"]) if x)
        meta.append((name, rec["COTE"], rec["ID_TRC"]))
    tree = STRtree(lines)
    log(f"{len(lines)} street sides")

    import shapely
    pts = shapely.points(np.array(ax), np.array(ay))
    (src, dst), _ = tree.query_nearest(pts, max_distance=MAX_SNAP_M, return_distance=True, all_matches=False)
    log(f"snapped {len(src)} / {len(pts)} anchors")

    by_line = defaultdict(list)
    for i, li in zip(src, dst):
        by_line[int(li)].append((lines[li].project(pts[i]), i))

    portions = []
    for li, anchors in by_line.items():
        line = lines[li]
        L = line.length
        panel_list, claims = [], []
        for pos, i in anchors:
            if akind[i] == 0:
                panel_list += [(pos, arrow, r) for arrow, r in apan[i]]
            else:  # a paid bay is about 7 m long, centred on its position
                claims += [(max(0.0, pos - BAY_HALF_M), min(L, pos + BAY_HALF_M), r, 1) for r in apan[i]]
        claims += [(a, b, r, 0) for a, b, r in claim_intervals(panel_list, L)]
        name, side, trc = meta[li]
        for a, b, rules, npol, nbay in cut_line(claims, L):
            seg = line if (a <= 0 and b >= L) else substring(line, a, b)
            if seg.geom_type != "LineString":
                continue
            xs, ys = seg.xy
            lon, lat = to_wgs.transform(list(xs), list(ys))
            coords = tuple(round(v, 6) for pair in zip(lon, lat) for v in pair)
            portions.append((name, side, trc, coords, intern(rules), npol, nbay))
    log(f"{len(portions)} curb segments, {len(rulesets)} distinct rule sets")

    data = {"built": time.strftime("%Y-%m-%dT%H:%M:%S"), "rulesets": rulesets, "portions": portions}
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out_path)
    log(f"wrote {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB) in {time.time() - t0:.0f}s")
    return data


if __name__ == "__main__":
    import sys
    build(sys.argv[1], sys.argv[2], sys.argv[3])
