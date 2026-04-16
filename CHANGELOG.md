# Changelog

All notable changes to this project will be documented here.

## [1.0.3] - 2026-04-16

### Added
- **Filtered totals**: a "Filtered total" figure is now displayed beneath each consumption and cost timeseries chart, showing the sum (kWh or £) across the currently filtered date range.
- **Tariff history table**: the flat current-rates table in the Tariff Details section has been replaced with a collapsible per-agreement history. Agreements are listed newest-first; each row is expandable to reveal the full rate breakdown (unit rate inc/exc VAT, standing charge p/day, £/day, and annualised £/year). Variable-rate tariffs (e.g. Agile) are flagged accordingly.

### Fixed
- **Date filter recalculates data**: applying the year/month dropdowns or a custom date range now re-aggregates bar chart data client-side from embedded daily records rather than merely zooming the axis. Tooltips and totals for partial periods (e.g. filtering to March 1–14 and viewing by month) now correctly reflect only the days within the selected range.

---

## [1.0.2] - 2026-04-15

### Fixed
- **Standing charge aggregation**: monthly (and weekly/yearly) standing charge totals were incorrect whenever any half-hourly consumption slots were missing from the data. The aggregation now counts distinct calendar days per period and multiplies by the daily rate, so every day always accrues exactly one full day's standing charge regardless of slot completeness.

### Changed
- **Weather location variables are now required**: `WEATHER_LAT`, `WEATHER_LON`, and `WEATHER_LOCATION` must be set in `env.ini` (or as CI secrets). The dashboard previously silently fell back to hardcoded Exeter, UK coordinates if these were absent; it now exits immediately with a clear error message.
- **Docs: local vs CI caching behaviour clarified**: the readme previously stated that CI runs perform an incremental refresh. In fact the data cache (`data/cache/`) is gitignored and not available to CI, so each GitHub Actions run performs a full history fetch from the API. The readme now explains this distinction and notes that a full fetch is well within the API's limits at one run per day.

---

## [1.0.1] - 2026-04-14

### Changed
- Sunshine hours now derived via the FAO-56 Angstrom-Prescott formula (`S = N × (Rs/Ra − 0.25) / 0.50`) rather than ERA5 pre-computed `sunshine_duration`. ERA5's coarse grid (~25 km) overestimates sunshine in cloudy maritime climates; the radiation-ratio approach is location-agnostic and produces realistic values everywhere.

---

## [1.0.0] — 2026-04-14

Initial release.

### Added
- **Consumption pipeline** (`pipeline/`): paginated Octopus Energy REST API fetch for electricity and gas, with local Parquet cache (incremental refresh on subsequent runs).
- **Cost enrichment**: full tariff history discovered automatically from account via GraphQL; unit rates matched to half-hourly intervals using `merge_asof` (correct for Agile time-of-use pricing).
- **Weather integration** (`weather/fetch_weather.py`): daily temperature (min/mean/max) and sunshine hours from Open-Meteo ERA5 archive; solar noon elevation computed from solar geometry; WMO 1991–2020 climate normals fetched and cached per location.
- **OLS energy model** (`models/energy_model.py`): separate electricity and gas models using HDD₁₅, min temperature, sunshine hours, solar elevation, and cyclical month encoding; R² and prediction intervals reported.
- **Interactive dashboard** (`dashboard/build_dashboard.py`): self-contained HTML export with Plotly charts (consumption over time, temperature scatter, sunshine scatter) and a monthly forecast tool pre-filled from climate normals.
- **GitHub Actions workflow**: daily automated dashboard rebuild and publish to GitHub Pages.
- **Location configurability**: `WEATHER_LAT`, `WEATHER_LON`, `WEATHER_LOCATION` in `env.ini` point weather fetching and the forecast tool at any location worldwide.
