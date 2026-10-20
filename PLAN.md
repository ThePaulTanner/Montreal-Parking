# Montreal Parking: High-Level Plan

## Goal
A self-hosted web app, run on the NAS in Docker, where I log in and see Montreal parking data on a map.

## Data (CC-BY 4.0, attribution required)
- **Ville de Montréal, "Signalisation (stationnement sur rue)"**: sign records, with a French free-text restriction in `DESCRIPTION_RPA`. About 144k records, refreshed daily. The city says some arrondissements are incomplete and Île-Bizard–Sainte-Geneviève has no data. The data is indicative only.
- **Poteaux de signalisation**: pole geometry. `POTEAU_ID_POT` links poles to signs.
- **Agence de mobilité durable**: about 19.5k paid spaces, with tariffs and regulations.

## Existing projects reviewed
| Project | What it is | Licence | Verdict |
|---|---|---|---|
| [ztao-sd/mtl-park-map](https://github.com/ztao-sd/mtl-park-map) | Web app. Polars ETL to parquet, FastAPI serving GeoJSON, React + Leaflet. Filters by time, day and month. Parses 1,521 sign codes. No DB. | None stated | Closest match to the target stack. Newer, with 2 commits. |
| [Sandeep-Bharti/parkMTL](https://github.com/Sandeep-Bharti/parkMTL) | Expo mobile app with MapLibre and PMTiles. A build-time pipeline parses sign text (99.36% coverage) into a rules dictionary and recolours the map by "can I park now". | None stated | Best parser design and best data pipeline. The mobile front end is not what we want. |
| [MarvensChery/NavMtl-Backend](https://github.com/MarvensChery/NavMtl-Backend) | Node/Express API plus a Python spaCy and regex sign parser. JWT auth. | None stated | Less useful. Its parser returns an ambiguous `False`. |
| [mudar/ParkCatcher](https://github.com/mudar/ParkCatcher) | PHP/CodeIgniter plus Android. Archived in 2018 and moved to GitLab. | GPL-3.0 | Old. Useful only as a reference for parsing edge cases. |
| [PirateVert/info-route-mtl](https://github.com/PirateVert/info-route-mtl) | Map of roadwork and temporary no-parking notices. | Not checked | Possible later layer for temporary restrictions. |

## Reuse constraint
Several of the most useful repos show no licence. With no licence, the default is all rights reserved, so we can't copy their code without permission.

Options:
1. Ask the authors (mtl-park-map, parkMTL) to add a licence or give permission.
2. Reimplement the same ideas from scratch.

The ideas are free to reuse:
- Parse each distinct sign string once, not per request.
- Resolve to the most restrictive rule.
- Show the raw sign text when parsing fails.
- Serve the map as static tiles.

The city data is CC-BY, so only attribution is needed.

## Proposed architecture
- **ETL job** (Python, polars): download the city CSVs on a schedule, parse the sign text into rule IDs, and write parquet and GeoJSON. This follows the mtl-park-map and parkMTL approach.
- **API** (FastAPI): serves viewport queries and the "can I park here at time T" evaluation. A read-only dataset needs no DB, as mtl-park-map showed.
- **Frontend** (React + MapLibre): MapLibre over Leaflet so the curb layer can be recoloured quickly. The basemap comes from OSM raster tiles at first, then self-hosted PMTiles of Montréal only (parkMTL's extract is about 59 MB).
- **Auth**: a simple login in the app (single user at first), or put the app behind the NAS reverse proxy with Authelia. Needs a decision, see below.
- **Deploy**: a Docker Compose stack of API, static frontend and a scheduled ETL container. It sits behind the NAS reverse proxy with HTTPS.

## Phases
1. **Data**: pull the datasets and inspect the real fields. Check what the geometry looks like and where coverage gaps are.
2. **Parser**: build the sign-text parser with tests against real distinct strings. Track coverage and unparsed strings.
3. **API and map**: viewport query, "park now" colouring and a time scrubber.
4. **Auth and deploy**: login, Docker Compose, reverse proxy, scheduled refresh.
5. **Extras**: paid spaces, temporary restrictions, saving my own parking spots.

## Open questions
1. Which NAS and container manager do you use (Synology, Unraid, TrueNAS, other)? Do you already run a reverse proxy or SSO?
2. Is this just for you, or will other people log in?
3. Is the goal "show all the city's signs", "can I park here now", or "remember where I parked"? Your wording says the first, but the answer changes the design.
4. Do you want me to contact the mtl-park-map and parkMTL authors about licences, or should we just reimplement from scratch?

## Caveats
- I couldn't reach donnees.montreal.ca from this sandbox, so the field names and resource URLs above come from the project READMEs, not from the dataset itself. Step 1 verifies them.
- I read the repo front pages only, not their source code, so the licence and parser-quality claims need checking in the code.
