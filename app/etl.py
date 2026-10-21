"""Daily data refresh (runs inside the app container as a background thread).

Download the city + Agence parking data, validate it, and publish it atomically.

Layout under DATA_DIR:
  releases/<UTC timestamp>/   one folder per successful run
  current -> releases/<ts>    symlink swapped only after validation passes
  current/.ok                 touched on success (used by the container healthcheck)
"""
import datetime as dt
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile

DATA = os.environ.get("DATA_DIR", "/data")
UA = os.environ.get("HTTP_USER_AGENT", "Mozilla/5.0 (compatible; mtl-parking/1.0)")
REFRESH_AT = os.environ.get("REFRESH_AT", "05:30")
MIN_RATIO = float(os.environ.get("MIN_ROWS_RATIO", "0.9"))
KEEP = 3

CKAN = "https://donnees.montreal.ca/api/3/action/package_show?id=stationnement-sur-rue-signalisation-courant"
GEO_CKAN = "https://donnees.montreal.ca/api/3/action/package_show?id=geobase-double-statique"
AMD = "https://www.agencemobilitedurable.ca/images/data/{}.csv"
AMD_FILES = ["Places", "Reglementations", "Periodes", "EmplacementReglementation",
             "ReglementationPeriode", "DateExportation"]
SIGN_COLUMNS = {"POTEAU_ID_POT", "POSITION_POP", "PANNEAU_ID_RPA", "DESCRIPTION_RPA",
                "FLECHE_PAN", "DESCRIPTION_CAT", "DESCRIPTION_REP", "Longitude", "Latitude"}


def log(*a):
    print(dt.datetime.now().isoformat(timespec="seconds"), *a, flush=True)


def get(url, dest=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:  # follows the redirect to cloud storage
        body = r.read()
    if dest:
        with open(dest, "wb") as f:
            f.write(body)
    return body


def count_lines(path):
    with open(path, "rb") as f:
        return sum(1 for _ in f) - 1


def previous_rows():
    try:
        with open(os.path.join(DATA, "current", "manifest.json")) as f:
            return json.load(f)["sign_rows"]
    except (OSError, ValueError, KeyError):
        return None


SHP_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")


def fetch_geobase(stage):
    """Download the street-sides shapefile (Géobase double) into stage/gbdouble/."""
    pkg = json.loads(get(GEO_CKAN))["result"]
    res = next(r for r in pkg["resources"] if r["format"].upper() == "ZIP")
    zpath = os.path.join(stage, "gbdouble.zip")
    get(res["url"], zpath)
    out = os.path.join(stage, "gbdouble")
    os.makedirs(out)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = os.path.basename(info.filename)  # flatten: never trust paths inside the zip
            if name.lower().endswith(SHP_PARTS):
                with z.open(info) as src, open(
                    os.path.join(out, "geobase_double" + os.path.splitext(name)[1].lower()), "wb") as dst:
                    shutil.copyfileobj(src, dst)
    os.remove(zpath)
    if not os.path.exists(os.path.join(out, "geobase_double.shp")):
        raise RuntimeError("Géobase double zip did not contain a shapefile")
    return out


def run_once():
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stage = os.path.join(DATA, "releases", ts + ".partial")
    os.makedirs(stage)
    try:
        pkg = json.loads(get(CKAN))["result"]
        res = next(r for r in pkg["resources"]
                   if r["format"].upper() == "CSV" and r["url"].endswith("signalisation_stationnement.csv"))
        sign = os.path.join(stage, "signalisation_stationnement.csv")
        get(res["url"], sign)

        # The portal can answer 200 with an error page, so check the content.
        with open(sign, "rb") as f:
            header = f.readline().decode("utf-8-sig").strip().split(",")
        missing = SIGN_COLUMNS - set(header)
        if missing:
            raise RuntimeError(f"signage file is not the expected CSV, missing columns {sorted(missing)}")
        rows = count_lines(sign)
        prev = previous_rows()
        if prev and rows < prev * MIN_RATIO:
            raise RuntimeError(f"signage has {rows} rows, under {MIN_RATIO:.0%} of the last good run ({prev})")

        for name in AMD_FILES:
            body = get(AMD.format(name), os.path.join(stage, name + ".csv"))
            if name != "DateExportation":
                body.decode("cp1252")  # Agence files are Windows-1252
                if len(body) < 1000 or body.lstrip()[:1] == b"<":
                    raise RuntimeError(f"{name}.csv looks wrong ({len(body)} bytes)")

        with open(os.path.join(stage, "DateExportation.csv")) as f:
            amd_date = f.read().split()[-1]

        from parking import curbs  # imported here so the downloader stays importable without geo libraries
        geo = fetch_geobase(stage)
        built = curbs.build(stage, geo, os.path.join(stage, "curbs.pkl"))
        shutil.rmtree(geo)
        n_curbs = len(built["portions"])
        if n_curbs < 50000:
            raise RuntimeError(f"only {n_curbs} curb segments were built; expected about 70,000")
        del built
        manifest = {"built_utc": ts, "sign_rows": rows, "curb_segments": n_curbs,
                    "city_last_modified": res.get("last_modified"), "agence_export_date": amd_date}
        with open(os.path.join(stage, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
        open(os.path.join(stage, ".ok"), "w").close()

        final = os.path.join(DATA, "releases", ts)
        os.rename(stage, final)
        tmp = os.path.join(DATA, "current.new")
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(os.path.join("releases", ts), tmp)
        os.replace(tmp, os.path.join(DATA, "current"))  # atomic swap
        log("published", ts, manifest)

        old = sorted(d for d in os.listdir(os.path.join(DATA, "releases")) if not d.endswith(".partial"))
        for d in old[:-KEEP]:
            shutil.rmtree(os.path.join(DATA, "releases", d), ignore_errors=True)
        return True
    except Exception as e:
        log("FAILED, keeping previous data:", repr(e))
        shutil.rmtree(stage, ignore_errors=True)
        return False


def seconds_until(hhmm):
    h, m = map(int, hhmm.split(":"))
    now = dt.datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return (t - now).total_seconds()


def daemon():
    """Fetch once if there is no data yet, then re-run every day at REFRESH_AT."""
    os.makedirs(os.path.join(DATA, "releases"), exist_ok=True)
    try:
        if not os.path.exists(os.path.join(DATA, "current")):
            run_once()  # first start: fetch straight away
        while True:
            wait = seconds_until(REFRESH_AT)
            log(f"next run in {wait/3600:.1f}h at {REFRESH_AT}")
            time.sleep(wait)
            if not run_once():
                time.sleep(1800)
                run_once()  # one retry after 30 minutes
    except Exception as e:  # never take the web server down with the downloader
        log("ETL loop crashed:", repr(e))


if __name__ == "__main__":
    os.makedirs(os.path.join(DATA, "releases"), exist_ok=True)
    sys.exit(0 if run_once() else 1)
