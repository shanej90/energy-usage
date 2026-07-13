# Changelog

All notable changes to this project will be documented here.

## [1.0.4] - 2026-07-11

- Added `update_dashboard.sh` — rebuilds the dashboard locally using the existing Parquet cache (so only new records get fetched) and serves `outputs/` at `http://localhost:8000` for a quick look, opening it in your default browser. Doesn't commit or push — commit `outputs/dashboard.html` yourself once you're happy with it.
- Dashboard now runs on the [Bootswatch Zephyr](https://bootswatch.com/zephyr/) theme. CSS rewritten around `--dash-*` custom properties (surface, muted, accent, border, text) mapped to Bootstrap theme variables, replacing the old hardcoded dark-slate palette. Filter bar gets a blue-to-teal gradient, cards and tables pick up the theme's border/shadow treatment, `#main` capped at 1480px and centred.
- Attribution footer now credits Bootswatch/Zephyr alongside Open-Meteo.

---

## [1.0.3] - 2026-04-16

- Added a "Filtered total" under each consumption/cost chart — the kWh or £ sum across whatever date range is currently filtered.
- Tariff Details section: swapped the flat current-rates table for a collapsible per-agreement history. Newest first, expand any row for the full rate breakdown (unit rate inc/exc VAT, standing charge p/day and £/day, annualised £/year). Variable tariffs like Agile get flagged.
- Fixed: the year/month dropdowns and custom date range now recalculate bar chart data client-side from the embedded daily records, instead of just zooming the axis. Tooltips and totals for partial periods (e.g. March 1–14 viewed by month) actually reflect only the filtered days now.

---

## [1.0.2] - 2026-04-15

- Fixed: monthly (and weekly/yearly) standing charge totals were wrong whenever any half-hourly slots were missing. Now counts distinct calendar days per period and multiplies by the daily rate, so every day accrues exactly one day's standing charge regardless of gaps.
- `WEATHER_LAT`, `WEATHER_LON` and `WEATHER_LOCATION` are now required in `env.ini` (or as CI secrets). Previously the dashboard fell back silently to Exeter, UK if these were missing — it now exits immediately with a clear error instead.
- Readme fix: it used to claim CI does an incremental refresh. Not true — `data/cache/` is gitignored, so every CI run does a full history fetch. Readme now says so, and notes that's fine at one run a day.

---

## [1.0.1] - 2026-04-14

- Sunshine hours now come from the FAO-56 Angstrom-Prescott formula (`S = N × (Rs/Ra − 0.25) / 0.50`) instead of ERA5's pre-computed `sunshine_duration`. ERA5's ~25 km grid overestimates sunshine in cloudy maritime climates; the radiation-ratio version is location-agnostic and works everywhere.

---

## [1.0.0] - 2026-04-14

Initial release.

- `pipeline/`: paginated Octopus Energy REST fetch for electricity and gas, with a local Parquet cache (incremental after the first run).
- Cost enrichment: full tariff history discovered automatically from your account via GraphQL; unit rates matched to half-hourly intervals with `merge_asof` (handles Agile time-of-use pricing correctly).
- `weather/fetch_weather.py`: daily temperature (min/mean/max) and sunshine hours from Open-Meteo ERA5, solar noon elevation from solar geometry, WMO 1991–2020 climate normals fetched and cached per location.
- `models/energy_model.py`: separate OLS models for electricity and gas using HDD₁₅, min temperature, sunshine hours, solar elevation and cyclical month encoding. Reports R² and prediction intervals.
- `dashboard/build_dashboard.py`: self-contained HTML export with Plotly charts (consumption over time, temperature scatter, sunshine scatter) and a monthly forecast tool pre-filled from climate normals.
- GitHub Actions workflow for a daily automated rebuild and publish to GitHub Pages.
- `WEATHER_LAT`, `WEATHER_LON`, `WEATHER_LOCATION` in `env.ini` — point weather fetching and the forecast tool at any location worldwide.
