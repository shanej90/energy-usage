# Changelog

All notable changes to this project will be documented here.

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
