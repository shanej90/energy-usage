# Energy usage with Octopus

Analysis of smart-meter electricity and gas usage via the [Octopus Energy API](https://developer.octopus.energy/).

TL;DR - View the [final output](https://shanej90.github.io/energy-usage/outputs/dashboard.html).

## Features

- Half-hourly consumption data fetched from the Octopus REST API with full pagination
- Local Parquet cache — full history is fetched once, then only new records get pulled
- Cost calculations: energy cost + standing charge (needs tariff codes)
- Aggregation to half-hour / hour / day / week / month / year
- Interactive Plotly charts with a period selector (Daily / Weekly / Monthly / Yearly)
- Date filter bar (year/month dropdowns and custom date range) that recalculates aggregated data client-side — tooltips and totals always reflect the filtered period
- Filtered total (kWh or £) shown beneath each consumption and cost chart
- Collapsible tariff history table — every agreement listed newest-first, expandable to the full rate breakdown
- Self-contained HTML dashboard export, ready for GitHub Pages
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

All values live on the [Octopus personal API access page](https://octopus.energy/dashboard/new/accounts/personal-details/api-access).

### 3. Build the dashboard

```bash
python dashboard/build_dashboard.py
```

First run fetches your full consumption history from the API (10–30s depending on history length) and writes Parquet files to `data/cache/`. After that, local runs load from the cache and only pull new records — under a second.

Produces a self-contained `outputs/dashboard.html`.

## Publishing to GitHub Pages

1. Commit `outputs/dashboard.html` to the repo
2. Go to **Settings → Pages** and set the source to the repo root (or `docs/` if you move the file there)
3. The dashboard will be live at `https://<user>.github.io/<repo>/outputs/dashboard.html`

## Automated daily updates (GitHub Actions)

A workflow at [`.github/workflows/update-dashboard.yml`](.github/workflows/update-dashboard.yml)
runs `dashboard/build_dashboard.py` every day at 07:00 UTC, commits the refreshed
`outputs/dashboard.html`, and pushes it — keeping the dashboard current automatically.

### Setup

**1. Add repository secrets**

Go to **Settings → Secrets and variables → Actions → New repository secret** and add each of these:

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

These map directly to the `[default]` keys in `env.ini`. The workflow writes a
temporary `env.ini` from them at runtime; it's never committed.

**2. Enable GitHub Pages**

Go to **Settings → Pages**, set the source branch to `main` and the folder to
`/ (root)`. After the first successful run the dashboard is live at:

```
https://<your-username>.github.io/<repo-name>/outputs/dashboard.html
```

**3. Trigger a first run**

Go to **Actions → Update energy dashboard → Run workflow** to trigger it immediately rather than waiting until 07:00 UTC.

### Notes

- Uses `[skip ci]` in the commit message so it doesn't trigger itself.
- `data/cache/` is gitignored, so CI has no cache and every run does a full history fetch. Local runs keep the Parquet cache and only pull new records. A full fetch takes 10–30s; well within API limits at one run a day.
- To change the schedule, edit the `cron` expression in the workflow file. [crontab.guru](https://crontab.guru/) is a useful reference.

## Notes

- **Tariff history** — pulled automatically from your account, so there's no tariff code to configure by hand. Switch tariff and the new one shows up on the next build.
- **Gas units** — SMETS2 meters report in m³, converted to kWh at ~11.1 kWh/m³. If your meter already reports kWh, set `GAS_IS_M3 = False` near the top of `dashboard/build_dashboard.py`.
- **Agile tariffs** — the unit-rate fetch returns one rate per 30-minute slot; `add_costs()` uses `merge_asof` to match each consumption interval to the right price.
- **SSL inspection** — `build_session()` merges the Windows certificate store into the certifi CA bundle, fixing TLS errors caused by corporate/antivirus SSL inspection.
- **Sunshine hours** — ERA5's pre-computed sunshine duration overestimates in cloudy maritime climates, because the reanalysis model runs on a coarse (~25 km) grid that smooths out cloud variability. Sunshine hours are instead estimated with the FAO-56 Angstrom-Prescott formula: `S = N × (Rs/Ra − 0.25) / 0.50`, where `N` is astronomical day length, `Rs` is daily shortwave radiation from ERA5, and `Ra` is extraterrestrial radiation — both computed from solar geometry. Location-agnostic: sunnier climates naturally produce higher values because their `Rs/Ra` ratio is genuinely higher.

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

## Theme

The dashboard theme is [Zephyr](https://bootswatch.com/zephyr/) from Bootswatch.

## AI

TLS enforcement issues accessing the GraphQL API resolved with Claude Code assistance. Readme augmented by Claude Code. Javascript for the final HTML output generated by Claude Code. All AI-generated code/documentation has been reviewed and tested.
