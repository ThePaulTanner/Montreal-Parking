"""Spike: can we tell which side of the street each sign pole is on?

Snaps every installed pole (DESCRIPTION_REP == 'Réel') to the nearest curb-side line in
the city's Géobase double, and reports how often that is unambiguous, plus how the
FLECHE_PAN arrow code relates to where the pole sits along its block.

Both datasets are in NAD83 / MTM zone 8 (metres), so no reprojection is needed.
Run: python spikes/street_sides.py  (needs pyshp, shapely, numpy)
"""
import collections
import csv
import sys

import numpy as np
import shapefile
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

SIGNS = "data/raw/signalisation_stationnement.csv"
SIDES = "data/geo/gbdouble/geobase_double.shp"
MAX_SNAP = 25.0  # metres

sf = shapefile.Reader(SIDES, encoding="utf-8")
lines, meta = [], []
for sr in sf.iterShapeRecords():
    pts = sr.shape.points
    if len(pts) < 2:
        continue
    lines.append(LineString(pts))
    meta.append(sr.record.as_dict())
tree = STRtree(lines)
print(f"{len(lines)} curb-side lines")

poles = {}
codes = collections.defaultdict(set)
for r in csv.DictReader(open(SIGNS, encoding="utf-8")):
    if r["DESCRIPTION_REP"] != "Réel":
        continue
    pid = r["POTEAU_ID_POT"]
    poles[pid] = (float(r["X"]), float(r["Y"]))
    codes[pid].add(r["FLECHE_PAN"])
print(f"{len(poles)} installed poles")

dists, margins, unsnapped = [], [], 0
pos_by_code = collections.defaultdict(list)  # arrow code -> position along block (0 start, 1 end)
side_counts = collections.Counter()
far_examples = []
for pid, (x, y) in poles.items():
    p = Point(x, y)
    idx = tree.query(p.buffer(60))
    if len(idx) == 0:
        unsnapped += 1
        continue
    d = sorted((lines[i].distance(p), i) for i in idx)
    if d[0][0] > MAX_SNAP:
        unsnapped += 1
        if len(far_examples) < 5:
            far_examples.append((pid, round(d[0][0], 1)))
        continue
    best_d, best = d[0]
    # runner-up on a different street segment/side
    other = next((dd for dd, i in d[1:] if meta[i]["COT_RUE_ID"] != meta[best]["COT_RUE_ID"]), None)
    dists.append(best_d)
    if other is not None:
        margins.append(other - best_d)
    side_counts[meta[best]["COTE"]] += 1
    t = lines[best].project(p) / lines[best].length
    if len(codes[pid]) == 1:
        pos_by_code[next(iter(codes[pid]))].append(t)

dists = np.array(dists)
margins = np.array(margins)
n = len(poles)
print(f"\nsnapped within {MAX_SNAP:.0f} m: {len(dists)} / {n} ({len(dists)/n:.1%}); unsnapped {unsnapped}")
print("snap distance m  p50 %.1f  p90 %.1f  p99 %.1f" % tuple(np.percentile(dists, [50, 90, 99])))
print("gap to the next-nearest other curb line (bigger = less ambiguous):")
for thr in (1, 3, 5):
    print(f"  gap > {thr} m: {(margins > thr).mean():.1%}")
print("side of nearest line:", dict(side_counts))
print("far examples (pole id, metres):", far_examples)
print("\nArrow code vs position along block (0 = start of line, 1 = end), poles with a single code:")
for c, v in sorted(pos_by_code.items(), key=lambda kv: -len(kv[1])):
    v = np.array(v)
    print(f"  FLECHE_PAN={c:>2}  n={len(v):6d}  mean {v.mean():.2f}  share in first 25% {(v<.25).mean():.0%}  last 25% {(v>.75).mean():.0%}")
