# Spike: which side of the street is each pole on?

Script: `spikes/street_sides.py`. Inputs: the city signage CSV and the city's
"Géobase double" shapefile (one line per street side, `COTE` = Gauche/Droite). Both use
NAD83 / MTM zone 8 in metres, so no reprojection is needed.
Géobase double: https://donnees.montreal.ca/dataset/geobase-double-statique (CC-BY 4.0).

Run (2026-10-21 data, 89,116 installed poles, 91,346 curb-side lines):
- 99.1% of poles snap to a curb line within 25 m; median 2.9 m, p99 8.5 m.
- Left/right split is 44,176 / 44,157.
- 93% have a next-nearest line from another block more than 3 m further away. The rest
  are mostly corners and need a closer look.
- `FLECHE_PAN` codes 2 and 3 are directional: code 2 poles sit mostly near the start of
  the block line (43% in the first quarter, 17% in the last), code 3 near the end
  (45% in the last quarter). Code 0 (about 49k poles) shows no skew.

Conclusion: assigning a pole to a street side works. Not yet known: which way code 2 and
3 point along the curb, and what code 0, 8, 10, 11 and 22 mean. Next step is to check
a few real streets by eye and use the start/end sign pairs of a zone as evidence.

Géobase double must be downloaded separately (the zip is 12 MB) into
`data/geo/gbdouble/`. The ETL does not fetch it yet.
