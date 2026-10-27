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

**Find parking:** pick a stay (30 minutes to 24 hours) and, optionally, "include paid". The map then shows
only curbs where you could park for that whole stay starting now (or at the time you picked): green for free,
blue for "fits if you pay" (with a rough cost), amber for "check the sign". The check looks ahead through the
whole stay, so a street-cleaning ban tomorrow morning rules out an overnight stay. A time limit counts only if
it is at least as long as your stay. In drive mode each side of the street says whether your stay fits, and if
not, why ("not now: no parking", or "only until 16:00, then no stopping"). The map refreshes as you move.

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
* `app/parking/accounts.py`, `app/cli.py` login (password + authenticator code), logins managed on the NAS
* `app/server.py`, `app/static/index.html` web app: sign-in, API, map, account page
* `docs/DATA.md`, `docs/SPIKE-street-sides.md` data notes and the street-side matching experiment

## Known limits
* **Sign arrows decide how far a sign reaches.** Arrow 3 points toward the end of the curb line and arrow 2 toward
  the start; a pair of such signs (same text) brackets the stretch between them, which is how a 2-space EV
  zone stays 2 spaces. A sign with no arrow governs the curb around it, up to 100 m each way or halfway to the
  next identical sign. Measured on the city data: for short reserved bays, 97% of pole pairs follow this pattern
  on both sides of the street. The meaning of the rare codes (8, 22, 10, 11, 1) is guessed as "no direction".
* **"No sign data" means the city has no sign recorded for that stretch, not that parking is allowed.** It is
  drawn grey, and Find parking shows it amber instead of green. The city's data lags the street and some
  boroughs are incomplete, so a stretch can have signs on the street that are not in the data.
* "Free, time limit" and "reserved" only describe the rule; they do not know your vehicle or permit.
  Permit areas are shown as "permit only" for everyone.
* Maintenance ("entretien", flexible dates) signs and about 1.4% of other texts are shown as "check sign".
* School-day signs are approximated as Monday to Friday, September to June.
* Data can differ from the street; some boroughs are incomplete.

## Sign-in and access (same model as HomeFinance)
* **Only reachable through Tailscale.** The container listens on `127.0.0.1` only; `tailscale serve` is the
  one way in, and it gives a real HTTPS certificate (which is also what lets the phone allow GPS).
* **Password + authenticator code**, or a one-time recovery code. Passwords are Argon2id with a secret
  pepper; an authenticator code can be used only once.
* **Logins are made on the NAS, never on the web:** `docker exec -it mtl-parking-app python cli.py create-user --username paul`
  prints a QR code and 10 recovery codes. The same tool has `unlock`, `users`, `log` and `delete-user`.
* Failed attempts are delayed, and after 10 in a row sign-in stays locked for everyone until `cli.py unlock`
  is run on the NAS. Every attempt is logged (never the password or code).
* Each device is its own session. `/settings` lists them with Revoke and shows the sign-in log. Sessions end
  after 8 hours without use (`SESSION_IDLE_MINUTES`) or 30 days.
* **Passkeys (Face ID):** on the Settings page (the Settings button at the top right), add one per device (it asks for your password again, because a
  passkey signs in without the authenticator code). The sign-in page then has a "Sign in with Face ID / passkey"
  button. A passkey sign-in is held to the same lock and logs as a password one.
* Not copied from HomeFinance: Plaid, which has nothing to do with parking.

## Run it on a Synology
See the header of `docker-compose.yml` and `.env.example`. In short: put the project folder on the NAS,
fill in `.env` (mainly `APP_SECRET`), create a Container Manager project from it, create your login with
`cli.py`, then `sudo tailscale serve --bg --https=10000 http://127.0.0.1:8480` and open
`https://YOUR-NAS.YOUR-TAILNET.ts.net:10000` on a phone or computer that has Tailscale.

## Version
`app/VERSION` holds the release number (MAJOR.MINOR.PATCH). It is shown under the title on the map page and on the
Settings page, with the short commit the image was built from, for example "Version 1.3.0 (build 0123abc)". Bump
it in every pull request: minor for a feature, patch for a fix.

## Develop
```
python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt pytest httpx
.venv/bin/python -m pytest tests
```
To build the data from files you downloaded: `cd app && python -m parking.curbs <raw_dir> <geobase_dir> <out.pkl>`.
