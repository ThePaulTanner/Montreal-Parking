# Data: sources, shape, refresh

> **Status: secondhand, unverified.** The sandbox could not reach donnees.montreal.ca or agencemobilitedurable.ca (network policy 403 / DNS). Everything below comes from the public README and `docs/data-notes.md` of [parkMTL](https://github.com/Sandeep-Bharti/parkMTL), plus search results. Step 1 of the build is to download the files and confirm each claim here against the real data.

Both sources are CC-BY 4.0. Attribution required:
> Données : Agence de mobilité durable de Montréal; Ville de Montréal (CC BY 4.0)

Both publishers say the data can differ from the street. The signs on the street are authoritative, and the app should say so.

## 1. Ville de Montréal: on-street signage ("Signalisation (stationnement sur rue)")
- Landing page: `https://donnees.montreal.ca/dataset/stationnement-sur-rue-signalisation-courant` (also mirrored on donneesquebec.ca).
- Format: CSV. Refreshed **daily**.
- Size (per parkMTL): 144,177 sign rows on 102,132 poles across 20 boroughs, with no missing coordinates. Île-Bizard–Sainte-Geneviève has no data, and some boroughs are incomplete.
- Sibling dataset "Poteaux de signalisation" (poles) shares the key `POTEAU_ID_POT`. The sign rows already carry coordinates, so we may not need it.

Columns that matter:
| Column | Meaning |
|---|---|
| `PANNEAU_ID_RPA` | Stable id of the sign text (1 to 17,021). Use as the rule id. Never use row position, because ids would reshuffle every refresh. |
| `DESCRIPTION_RPA` | French free-text restriction. About 1,525 distinct strings. The top 100 cover 72% and the top 800 cover 98%. |
| `DESCRIPTION_CAT` | Category. Keep `STATIONNEMENT`, `STAT-$` and `STAT. HORS RUE`. |
| `DESCRIPTION_REP` | Lifecycle state. **Keep only `Réel`** (118,359 rows). `Enlevé` (12,209), `En conception` (8,283) and `Archive` (5,326) are not on the street. |
| `POSITION_POP` | Panel order on a pole, top to bottom. Sub-panels (`PANONCEAU …`, `AUTOCOL. …`) modify the panel above and can't be evaluated alone. |
| `FLECHE_PAN` | Which direction along the curb the sign governs (0, 2, 3 and others). |
| `POTEAU_ID_POT` | Pole id. |
| `Latitude`, `Longitude` | Pole position. |

Fetch traps:
- The portal answers default HTTP clients with `RBAC: access denied` **with HTTP 200**. Send a browser `User-Agent` and check the body, or you will save the error page as a valid CSV.
- Prefer the CKAN API (`/api/3/action/package_show?id=...`) to find the current resource URL, so the job doesn't hard-code a file path.

## 2. Agence de mobilité durable: paid on-street parking
- Landing page: `https://www.agencemobilitedurable.ca/fr/informations/donnees-ouvertes/description-des-donnees-disponibles`
- Files: `https://www.agencemobilitedurable.ca/images/data/<name>.csv`. Tables: `Places`, `Reglementations`, `Periodes`, `EmplacementReglementation`, `ReglementationPeriode`, `DateExportation`.
- About 19,573 spaces, with about 4.5 regulations each. Refresh cadence is not stated. `DateExportation.csv` (19 bytes) holds the export date, which makes it a cheap freshness check.

Traps:
- **Encoding is Windows-1252, not UTF-8.** Read as UTF-8, accented values like `HANDICAPÉ` stop matching.
- Tariffs are integer cents (`425` = $4.25/h).
- `sType = Double` means two bays share one pole.
- Use `nPositionCentreLongitude/Latitude` (the bay centre) for map placement, not `nLongitude/nLatitude` (the pole).
- `Reglementations.sType` codes are undocumented. parkMTL inferred them: I and A no parking, R no stopping, U paid, Q and P maximum duration, G free, D and M daily rate or maximum, H disabled-only, B EV charging, Z and F notices. We should confirm this ourselves.
- `Periodes.csv` is fully structured (start and end time plus seven weekday booleans), so no text parsing is needed. Don't parse `ReglementationPeriode` descriptions.

## 3. Sign-text grammar (what the parser must handle)
An action marker (`\P` no parking, `\A` no stopping, `P` parking permitted), then optional duration, time ranges, weekdays, an optional `ET` clause, season, and `EXCEPTE` exemptions.

Known traps:
- `P 02h 09h-18h` must not read `02h 09` as 2:09.
- A range that crosses midnight takes the weekday it starts on.
- `\P AUX CAMIONS` (applies to trucks only) differs from `\P EXCEPTE MOTOS` (everything except motorcycles).
- A bare `\P DEUX COTES` is unconditional, not unparseable.
- `MARS` (month) is not `MAR` (Tuesday). Extract seasons before weekdays.
- Typos and run-together tokens exist in the source (`AVIL`, `LUNDRI`, doubled backslashes).
- Fall back to showing the literal sign text when parsing fails (about 0.4% of signs for parkMTL).

## 4. Refresh design (proposal)
1. A scheduled container runs the ETL once a day, a few hours after the city's refresh.
2. Resolve the current resource URL through the CKAN API, then download with a browser `User-Agent`.
3. Validate before swapping: the body isn't `RBAC: access denied`, the row count is within about 10% of the last good run, expected columns are present, and `Réel` rows are non-empty. On failure, keep serving yesterday's data and log the error.
4. Parse only sign strings not already seen, and cache the rule dictionary keyed by `PANNEAU_ID_RPA` plus the text hash. Log any new unparsed strings.
5. Write to a new versioned parquet directory and swap atomically. The API reloads on a signal or by polling the version file.
6. Poll the Agence's `DateExportation.csv` and re-download only when it changes.
7. Store the source `last_modified` and our build time and show them in the UI, so stale data is obvious.

## 5. First tasks (once network access allows)
- Allow `donnees.montreal.ca` (and `donneesquebec.ca` as a fallback) and `www.agencemobilitedurable.ca` in the environment's network settings.
- Download all files, confirm the column names and counts above, and record real sample rows.
- Check how `FLECHE_PAN` and `POSITION_POP` should become curb-side geometry. This is the biggest unknown for drawing signs on a map.
