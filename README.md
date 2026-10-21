# Montreal Parking

A private, self-hosted map of Montréal street parking. Log in, pan around the city, and see for each
**side of each street** whether you can park, what it costs, and until when. Drive mode follows your
phone's GPS and shows both curbs of the street you are on.

Built from open data (CC BY 4.0): Ville de Montréal (on-street signs, street network) and Agence de
mobilité durable (paid parking tariffs and rules). The signs on the street are always authoritative.

## What you see
Each curb segment is coloured for the chosen time (now, or any time you pick):
free, free with a time limit, paid (with the hourly rate), permit holders only, reserved (disabled,
electric vehicles, taxis...), no parking, no stopping (dashed), or "check the sign" when the sign text
could not be understood. Filter chips show or hide each type. Tapping a curb lists the raw sign texts.

## How it works
```
city CSV (signs) ──┐
Agence CSVs (paid) ┼─► parse sign text ─► snap to street sides ─► curb segments (curbs.pkl)
Géobase double ────┘   (app/parking/signparse.py)  (app/parking/curbs.py)
                                        │
                       web app (FastAPI) ◄───────┘  evaluates the rules for any time on request
```
One container does everything: it serves the site and, in a background thread, re-downloads and
rebuilds the data every day (05:30 Montréal time). A new build replaces the old one only after it passes
checks (columns present, row counts within 10% of the last good run); otherwise yesterday's data stays.

* `app/etl.py` download, validate, publish
* `app/parking/signparse.py` French sign text to rules (about 98.6% of installed signs fully understood)
* `app/parking/rules.py` rule model, "what applies at this minute", next change
* `app/parking/agence.py` paid bay rules from the Agence tables
* `app/parking/curbs.py` match poles and bays to street sides, cut curbs into segments
* `app/server.py`, `app/static/index.html` login, API, map
* `docs/DATA.md`, `docs/SPIKE-street-sides.md` data notes and the street-side matching experiment

## Known limits
* **Sign arrows are not used yet.** A sign is assumed to govern the stretch of curb around its pole,
  not "from here in this direction". Near block ends the status can be wrong. This is the biggest
  accuracy gap.
* Streets with no sign or bay in the data are not drawn (no information, not "free").
* "Free, time limit" and "reserved" only describe the rule; they do not know your vehicle or permit.
  Permit areas are shown as "permit only" for everyone.
* Maintenance ("entretien", flexible dates) signs and about 1.4% of other texts are shown as "check sign".
* School-day signs are approximated as Monday to Friday, September to June.
* Data can differ from the street; some boroughs are incomplete.

## Run it on a Synology
See the header of `docker-compose.yml` and `.env.example`. In short: copy the project folder to the NAS,
create `.env` (user, password hash, session secret), create a Container Manager project from it, and put
the DSM reverse proxy (HTTPS) in front of port 8480. Drive mode needs HTTPS for the phone's location.

## Develop
```
python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt pytest httpx
.venv/bin/python -m pytest tests
```
To build the data from files you downloaded: `cd app && python -m parking.curbs <raw_dir> <geobase_dir> <out.pkl>`.
