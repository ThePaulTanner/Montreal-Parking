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

**Where you parked:** tap the **Park here** button (bottom-right, above Drive mode). It saves your phone's GPS
position, drops a red P pin on the map and names the street. The button then becomes a green **Back to car**:
one tap opens your phone's own maps app with walking directions to the car (Apple Maps on an iPhone or iPad,
your default maps app on Android, Google Maps on a computer). Tap the pin for a card with how long ago you
parked, a note field (level, landmark), **Move here** and **Clear**. One spot is kept per login, on the server
(`parked.db` in the data folder), so your phone and your computer see the same spot. GPS needs the https
(Tailscale) address; if the location can't be read it offers to save the centre of the map instead.

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
* `app/parking/accounts.py`, `app/cli.py` login (passkey first, password fallback, optional authenticator), logins managed on the NAS
* `app/parking/spot.py` the saved parking spot, one per login
* `app/server.py`, `app/static/index.html` web app: sign-in, API, map, settings page
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

## Sign-in and access
* **Only reachable through Tailscale.** The container listens on `127.0.0.1` only; `tailscale serve` is the
  one way in, and it gives a real HTTPS certificate (which is also what lets the phone allow GPS and passkeys).
* **Passkey first, password as the fallback.** The normal way in is a passkey (Face ID or Touch ID), set up once
  per device in Settings. After a browser has used one, the sign-in page leads with a big Face ID button and
  tucks the password away under "Use your password". The map page offers to set one up if you have none.
* **No authenticator app by default.** Because the password is then the only other thing between a device on the
  tailnet and the app, a login without an authenticator needs a password of **14 or more characters** (a few
  random words is fine). Passwords are Argon2id with a secret pepper.
* **An authenticator is optional.** `cli.py create-user --with-authenticator` (or `add-authenticator` later) makes
  a login also ask for a code, with 10 one-time recovery codes. A login that has one shows the code under
  "My login also has an authenticator code" on the sign-in page.
* **Logins and recovery happen on the NAS, never on the web:**
  ```
  docker exec -it mtl-parking-app python cli.py create-user --username paul
  docker exec -it mtl-parking-app python cli.py reset-password --username paul        # forgot the password
  docker exec -it mtl-parking-app python cli.py remove-authenticator --username paul  # lost the authenticator app
  docker exec -it mtl-parking-app python cli.py add-authenticator --username paul     # turn the extra code on
  docker exec -it mtl-parking-app python cli.py users | log | unlock | delete-user --username paul
  ```
  `reset-password` and `remove-authenticator` keep your passkeys and sign the login out everywhere.
* Failed attempts are delayed, and after 10 in a row sign-in stays locked for everyone until `cli.py unlock`
  is run on the NAS. Every attempt is logged (never the password or code). Errors never reveal whether a name
  exists or has an authenticator.
* Each device is its own session. `/settings` lists them with Revoke and shows the sign-in log. Sessions end
  after 8 hours without use (`SESSION_IDLE_MINUTES`) or 30 days.
* Adding a passkey asks for your password once, to confirm it is you. A passkey sign-in is held to the same lock
  and logs as a password one.

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
