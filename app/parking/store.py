"""In-memory curb store: viewport queries, status at a time, and "what is here" for drive mode."""
import math
import os
import pickle
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .rules import GO, GO_PAID, MAYBE, NO, Fit, Status, evaluate, next_change, stay_check

NODATA = "nodata"  # a stretch of curb that no sign or bay claims: the city data says nothing about it
_NO_DATA_STATUS = Status(NODATA, "No sign data here")

TZ = ZoneInfo("America/Toronto")
CELL = 0.004  # degrees, about 300-450 m
M_LAT = 110_540.0
M_LON_EQ = 111_320.0


def local_time(iso=None):
    """Parse an ISO timestamp (with or without offset) to Montréal local time; None = now."""
    if not iso:
        return datetime.now(TZ).replace(tzinfo=None)
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(TZ).replace(tzinfo=None)


class Store:
    def __init__(self, path):
        self.path = path
        self.loaded_from = None
        self.portions = []
        self.rulesets = []
        self.built = None
        self._grid = {}
        self._checked = 0.0

    # --- loading -------------------------------------------------------------------------
    def refresh(self, force=False):
        """Reload when the `current` release changed on disk. Cheap; call on each request."""
        now = time.time()
        if not force and now - self._checked < 30:
            return
        self._checked = now
        try:
            real = os.path.realpath(self.path)
            if real == self.loaded_from or not os.path.exists(real):
                return
            with open(real, "rb") as f:
                data = pickle.load(f)  # written by our own ETL, never user supplied
        except (OSError, pickle.UnpicklingError, EOFError):
            return
        grid = {}
        for i, p in enumerate(data["portions"]):
            c = p[3]
            lons, lats = c[0::2], c[1::2]
            for ix in range(int(min(lons) // CELL), int(max(lons) // CELL) + 1):
                for iy in range(int(min(lats) // CELL), int(max(lats) // CELL) + 1):
                    grid.setdefault((ix, iy), []).append(i)
        self.portions, self.rulesets, self.built = data["portions"], data["rulesets"], data["built"]
        self._grid = grid
        self.loaded_from = real

    @property
    def ready(self):
        return bool(self.portions)

    # --- helpers -------------------------------------------------------------------------
    def _status(self, rid, dt, cache):
        key = rid
        if key not in cache:
            cache[key] = evaluate(self.rulesets[rid], dt)
        return cache[key]

    def _cells(self, west, south, east, north):
        for ix in range(int(west // CELL), int(east // CELL) + 1):
            for iy in range(int(south // CELL), int(north // CELL) + 1):
                yield from self._grid.get((ix, iy), ())

    # --- viewport ------------------------------------------------------------------------
    def viewport(self, west, south, east, north, dt, limit=40000, stay=None, paid=False):
        """GeoJSON-ready features for the curb segments touching the box, with status at `dt`.

        With `stay` (minutes) only curbs where a stay of that length starting at `dt` fits are returned,
        marked go (free the whole time), paid (fits if you pay; only when `paid`) or maybe (check the sign).
        """
        cache, seen, feats = {}, set(), []
        for i in self._cells(west, south, east, north):
            if i in seen:
                continue
            seen.add(i)
            name, side, trc, c, rid, npol, nbay = self.portions[i]
            lons, lats = c[0::2], c[1::2]
            if max(lons) < west or min(lons) > east or max(lats) < south or min(lats) > north:
                continue
            nodata = npol == 0 and nbay == 0
            if stay:
                if nodata:
                    fit = Fit(MAYBE)  # nothing recorded: never offer it as free, ask for a look at the street
                else:
                    if rid not in cache:
                        cache[rid] = stay_check(self.rulesets[rid], dt, stay, paid)
                    fit = cache[rid]
                if fit.result == NO:
                    continue
                props = {"i": i, "s": fit.result}
            elif nodata:
                props = {"i": i, "s": NODATA}
            else:
                s = self._status(rid, dt, cache)
                props = {"i": i, "s": s.status}
                if s.cents is not None:
                    props["c"] = s.cents
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "LineString", "coordinates": list(zip(lons, lats))}})
            if len(feats) >= limit:
                break
        return {"type": "FeatureCollection", "features": feats}

    # --- one segment ---------------------------------------------------------------------
    def describe(self, i, dt, stay=None, paid=False):
        name, side, trc, c, rid, npol, nbay = self.portions[i]
        rules = self.rulesets[rid]
        nodata = npol == 0 and nbay == 0
        s = _NO_DATA_STATUS if nodata else evaluate(rules, dt)
        when, nxt = (None, None) if nodata else next_change(rules, dt)
        info = {
            "id": i, "street": name, "side": side,
            "status": s.status, "detail": s.detail, "cents": s.cents, "limit_min": s.limit_min,
            "until": when.isoformat() if when else None,
            "then": {"status": nxt.status, "detail": nxt.detail} if nxt else None,
            "signs": sorted({r.text for r in rules if r.text}),
            "poles": npol, "bays": nbay,
        }
        if stay:
            f = Fit(MAYBE) if nodata else stay_check(rules, dt, stay, paid)
            info["fit"] = {"result": f.result, "at": f.at.isoformat() if f.at else None,
                           "status": f.status, "detail": f.detail, "cost_cents": f.cost_cents}
        return info

    # --- drive mode ----------------------------------------------------------------------
    def here(self, lat, lon, heading, dt, radius_m=45, stay=None, paid=False):
        """The nearest street segment's two sides, labelled left/right of travel when heading is known."""
        lat0 = lat
        kx = M_LON_EQ * math.cos(math.radians(lat0))

        def to_xy(lo, la):
            return (lo - lon) * kx, (la - lat) * M_LAT

        best = {}  # portion index -> (distance_m, nearest_xy)
        dlat, dlon = radius_m / M_LAT, radius_m / kx
        for i in set(self._cells(lon - dlon, lat - dlat, lon + dlon, lat + dlat)):
            c = self.portions[i][3]
            pts = [to_xy(c[k], c[k + 1]) for k in range(0, len(c), 2)]
            d, near = _dist_to_polyline(pts)
            if d <= radius_m:
                best[i] = (d, near)
        if not best:
            return {"found": False}
        i0 = min(best, key=lambda k: best[k][0])
        trc = self.portions[i0][2]
        sides = {}
        for i, (d, near) in best.items():
            name, side, t, *_ = self.portions[i]
            if t != trc:
                continue
            if side not in sides or d < sides[side][0]:
                sides[side] = (d, i, near)
        out = []
        for side, (d, i, near) in sides.items():
            info = self.describe(i, dt, stay, paid)
            info["distance_m"] = round(d, 1)
            if heading is not None:
                hx, hy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
                cross = hx * near[1] - hy * near[0]
                info["relative"] = "right" if cross < 0 else "left"
            out.append(info)
        out.sort(key=lambda r: r["distance_m"])
        return {"found": True, "street": self.portions[i0][0], "sides": out}


def _dist_to_polyline(pts):
    best, near = float("inf"), (0.0, 0.0)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / L2))
        px, py = x1 + t * dx, y1 + t * dy
        d = math.hypot(px, py)
        if d < best:
            best, near = d, (px, py)
    return best, near
