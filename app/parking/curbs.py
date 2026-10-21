"""Build curb segments: every street side cut into pieces that each carry the rules of the
nearest sign pole or paid bay.

Inputs
  signalisation_stationnement.csv   city signs, one row per panel, with the pole's X/Y (MTM zone 8, metres)
  Agence CSV tables                 paid bays (lon/lat), see agence.py
  geobase_double.shp                city "Géobase double": one polyline per street side (MTM zone 8)

Method (validated by spikes/street_sides.py: 99% of installed poles are within 25 m of a curb line)
  1. Parse each distinct sign text once, attach panonceaux to the panel above, group panels by pole.
  2. Snap every pole and bay to the nearest curb line (<= 25 m) and note its distance along the line.
  3. On each line, cut at the midpoints between consecutive anchors; each piece takes the rules of
     its anchor. Neighbouring pieces with identical rules are merged.
Known limit: the arrow on a sign (FLECHE_PAN) is not used yet, so a sign is assumed to govern the
stretch of curb around its pole, not a stretch in one direction from it.
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
SIGN_CATS = {"STATIONNEMENT", "STAT-$"}  # STAT. HORS RUE is off-street


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _apply_modifiers(panels):
    """Fold panonceaux into the panel above them; return the real panels only."""
    out = []
    for r in panels:
        if r.kind == MODIFIER:
            if out:
                prev = out[-1]
                out[-1] = replace(
                    prev,
                    exceptions=tuple(sorted(set(prev.exceptions) | set(r.exceptions))),
                    date_from=prev.date_from or r.date_from,
                    date_to=prev.date_to or r.date_to)
            continue
        out.append(r)
    return out


def load_poles(sign_csv):
    """{pole_id: (x, y, (Rule, ...))} for installed on-street poles."""
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
            panels[pid].append((int(row["POSITION_POP"] or 0), parse_cache[text]))
    poles = {}
    for pid, items in panels.items():
        items.sort(key=lambda it: it[0])
        rules = tuple(r for r in _apply_modifiers([r for _, r in items]) if r.kind != NOT_APPLICABLE)
        poles[pid] = (*xy[pid], rules)
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

    ax, ay, aid, akind = [], [], [], []
    for x, y, rules in poles.values():
        ax.append(x), ay.append(y), aid.append(intern(rules)), akind.append(0)
    if bays:
        bx, by = to_mtm.transform([b["lon"] for b in bays], [b["lat"] for b in bays])
        for x, y, b in zip(bx, by, bays):
            ax.append(x), ay.append(y), aid.append(intern(b["rules"])), akind.append(1)

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
        pos = lines[li].project(pts[i])
        by_line[int(li)].append((pos, aid[i], akind[i]))

    portions = []
    for li, anchors in by_line.items():
        line = lines[li]
        L = line.length
        anchors.sort()
        cuts = [0.0] + [(anchors[k][0] + anchors[k + 1][0]) / 2 for k in range(len(anchors) - 1)] + [L]
        pieces = []  # [start, end, ruleset_id, n_poles, n_bays]
        for k, (pos, rid, kind) in enumerate(anchors):
            a, b = cuts[k], cuts[k + 1]
            if pieces and pieces[-1][2] == rid:
                pieces[-1][1] = b
                pieces[-1][3 + kind] += 1
            else:
                pieces.append([a, b, rid, 1 - kind, kind])
        name, side, trc = meta[li]
        for a, b, rid, npol, nbay in pieces:
            if b - a < 0.5:
                continue
            seg = line if (a <= 0 and b >= L) else substring(line, a, b)
            if seg.geom_type != "LineString":
                continue
            xs, ys = seg.xy
            lon, lat = to_wgs.transform(list(xs), list(ys))
            coords = tuple(round(v, 6) for pair in zip(lon, lat) for v in pair)
            portions.append((name, side, trc, coords, rid, npol, nbay))
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
