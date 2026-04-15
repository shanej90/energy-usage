# Energy usage with Octopus

Analysis of smart-meter electricity and gas usage via the [Octopus Energy API](https://developer.octopus.energy/). 

TL;DR - View the [final output](https://shanej90.github.io/energy-usage/outputs/dashboard.html).

## Features

- Half-hourly consumption data fetched from the Octopus REST API with full pagination
- Local Parquet cache — full history is fetched once, then only new records are pulled
- Cost calculations: energy cost + standing charge (requires tariff codes)
- Aggregation to half-hour / hour / day / week / month / year
- Interactive Plotly charts with a period selector (Daily / Weekly / Monthly / Yearly)
- Self-contained HTML dashboard export suitable for GitHub Pages
- Weather integration: daily temperature and calibrated sunshine hours via Open-Meteo
- OLS energy model with a monthly consumption forecast tool

## Project structure

```
pipeline/
  fetch.py                 # Paginated REST API calls — consumption + tariff rates
  transform.py             # DataFrame construction, cost enrichment, aggregations

utils/
  auth.py                  # Session builder (Windows SSL fix) + Kraken token helper
  cache.py                 # Parquet-based local cache
  directory_navigation.py  # Project-root finder

weather/
  fetch_weather.py         # Open-Meteo ERA5 fetch; Angstrom-Prescott sunshine hours;
                           #   solar noon elevation; climate normals (WMO 1991–2020)

models/
  energy_model.py          # OLS regression — electricity & gas vs weather features

dashboard/
  build_dashboard.py       # Headless build script — produces outputs/dashboard.html

notebooks/
  auth.ipynb               # Authentication exploration (reference only)

data/cache/                # Auto-created; gitignored (re-fetchable from API)
outputs/                   # HTML dashboard export (commit this for GitHub Pages)
```

## Setup

### 1. Install dependencies

See `requirements.txt`.

### 2. Create `env.ini`

```ini
[default]
OCTOPUS_API_KEY      = your_api_key
ELECTRICITY_MPAN     = your_mpan
ELECTRICITY_SERIAL   = your_electricity_meter_serial
GAS_MPRN             = your_mprn
GAS_SERIAL           = your_gas_meter_serial

# Required — location for weather/climate calculations
WEATHER_LAT          = your_latitude
WEATHER_LON          = your_longitude
WEATHER_LOCATION     = your_location_name
```

All values can be found on the [Octopus personal API access page](https://octopus.energy/dashboard/new/accounts/personal-details/api-access).

### 3. Build the dashboard

```bash
python dashboard/build_dashboard.py
```

The first run fetches your full consumption history from the API (10–30 s depending
on history length) and writes Parquet files to `data/cache/`.  Subsequent local runs
load from that cache and only pull new records from the API, completing in under a second.

A self-contained `outputs/dashboard.html` is produced at the end.

## Publishing to GitHub Pages

1. Commit `outputs/dashboard.html` to the repo
2. Go to **Settings → Pages** and set the source to the repo root (or `docs/` if you move the file there)
3. The dashboard will be live at `https://<user>.github.io/<repo>/outputs/dashboard.html`

## Automated daily updates (GitHub Actions)

A workflow at [`.github/workflows/update-dashboard.yml`](.github/workflows/update-dashboard.yml)
runs `dashboard/build_dashboard.py` every day at 07:00 UTC, commits the refreshed
`outputs/dashboard.html`, and pushes it — keeping the GitHub Pages dashboard up to date automatically.

### Setup

**1. Add repository secrets**

Go to **Settings → Secrets and variables → Actions → New repository secret** and add each of the following:

| Secret name | Value |
|---|---|
| `OCTOPUS_API_KEY` | Your API key |
| `ELECTRICITY_MPAN` | Your MPAN |
| `ELECTRICITY_SERIAL` | Electricity meter serial |
| `GAS_MPRN` | Your MPRN |
| `GAS_SERIAL` | Gas meter serial |
| `WEATHER_LAT` | Latitude of your location |
| `WEATHER_LON` | Longitude of your location |
| `WEATHER_LOCATION` | Display name for your location |

These map directly to the `[default]` keys in `env.ini`.  The workflow writes a
temporary `env.ini` from them at runtime; the file is never committed.

**2. Enable GitHub Pages**

Go to **Settings → Pages**, set the source branch to `main` and the folder to
`/ (root)`.  After the first successful run the dashboard is live at:

```
https://<your-username>.github.io/<repo-name>/outputs/dashboard.html
```

**3. Trigger a first run**

Go to **Actions → Update energy dashboard → Run workflow** to run it immediately
rather than waiting until 07:00 UTC.

### Notes

- The workflow uses `[skip ci]` in its commit message to prevent triggering itself.
- The data cache (`data/cache/`) is gitignored and never committed, so each CI run
  starts without a cache and performs a full history fetch from the Octopus API.
  This differs from local runs, where the Parquet cache persists between builds and
  only new records are pulled.  A full fetch takes 10–30 s depending on history
  length; at one run per day this is well within the API's limits.
- To change the schedule, edit the `cron` expression in the workflow file.
  [crontab.guru](https://crontab.guru/) is a useful reference.

## Notes

- **Tariff history**: All past and present tariffs are discovered automatically from your account — no manual tariff code configuration is needed.  When you switch tariff, the new one is picked up on the next dashboard build.
- **Gas units**: SMETS2 meters report in m³ and are converted to kWh using a ~11.1 kWh/m³ factor.  Set `GAS_IS_M3 = False` near the top of `dashboard/build_dashboard.py` if your meter already reports kWh.
- **Agile tariffs**: The unit-rate fetch returns one rate per 30-minute slot; `add_costs()` uses `merge_asof` to correctly match each consumption interval to its price.
- **SSL inspection**: The `build_session()` function merges the Windows certificate store into the certifi CA bundle, resolving TLS errors caused by corporate/antivirus SSL inspection.
- **Sunshine hours**: ERA5's pre-computed sunshine duration systematically overestimates in cloudy maritime climates because the reanalysis model operates on a coarse (~25 km) grid that smooths out cloud variability.  Sunshine hours are instead estimated using the FAO-56 Angstrom-Prescott formula: `S = N × (Rs/Ra − 0.25) / 0.50`, where `N` is astronomical day length, `Rs` is daily shortwave radiation from ERA5, and `Ra` is extraterrestrial radiation — both computed from solar geometry.  This is location-agnostic: sunnier climates naturally produce higher values because their `Rs/Ra` ratio is genuinely higher.

## Env variables

| Variable | Required | Description |
|---|---|---|
| `OCTOPUS_API_KEY` | Yes | API key from Octopus personal details page |
| `ELECTRICITY_MPAN` | Yes | Meter Point Administration Number |
| `ELECTRICITY_SERIAL` | Yes | Electricity meter serial number |
| `GAS_MPRN` | Yes | Meter Point Reference Number |
| `GAS_SERIAL` | Yes | Gas meter serial number |
| `WEATHER_LAT` | Yes | Latitude for weather/climate calculations |
| `WEATHER_LON` | Yes | Longitude for weather/climate calculations |
| `WEATHER_LOCATION` | Yes | Display name for the weather location |

## References

- [Octopus REST API docs](https://developer.octopus.energy/)
- [Octopus GraphQL docs](https://docs.octopus.energy/graphql/)
- [Example downloader](https://github.com/OllieJC/oebd/blob/main/downloader.py) (credit OllieJC)

## AI

TLS enforcement issues in accessing GraphQL API resolved with Claude Code assistance. Readme augmented by Claude Code. Javascript for final HTML output generated by Claude Code. All AI-generated code/documentation has been reviewed and tested.
