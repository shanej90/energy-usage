"""
Weather data fetcher using the Open-Meteo free API.

Retrieves daily temperature (min / mean / max) and sunshine duration for a
configurable location.  Also computes the solar noon elevation angle for each
date (purely mathematical — no external dependency required).

Data source
-----------
Open-Meteo (https://open-meteo.com) — completely free, no API key required.
The ERA5-based archive endpoint is used for historical data.

To change location
------------------
Set WEATHER_LAT, WEATHER_LON, and WEATHER_LOCATION in env.ini [default]
(or pass them directly to load_or_fetch_weather).  The defaults are Exeter UK.

    [default]
    WEATHER_LAT      = 50.7236
    WEATHER_LON      = -3.5275
    WEATHER_LOCATION = Exeter, UK

Columns returned
----------------
date                 : date (UTC midnight, tz-naive)
temp_max             : float  — daily maximum 2 m air temperature (°C)
temp_min             : float  — daily minimum 2 m air temperature (°C)
temp_mean            : float  — daily mean 2 m air temperature (°C)
sunshine_hours       : float  — estimated sunshine hours (Angstrom-Prescott,
                         derived from shortwave_radiation_sum vs Ra)
solar_elevation_deg  : float  — solar noon elevation angle (degrees above horizon)
"""

import math
import os
import sys

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from utils import cache as C

# ── Default location: Exeter, UK ─────────────────────────────────────────────
DEFAULT_LAT      = 50.7236
DEFAULT_LON      = -3.5275
DEFAULT_LOCATION = "Exeter, UK"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CACHE_NAME  = "weather_daily"
_DAILY_VARS = ("temperature_2m_max,temperature_2m_min,"
               "temperature_2m_mean,shortwave_radiation_sum")

# The archive API has a ~5-day lag before data is finalised.
_ARCHIVE_LAG_DAYS = 6

# Angstrom-Prescott constants (FAO-56 standard values, dimensionless)
# S = N × (Rs/Ra − _AP_A) / _AP_B   clamped to [0, N]
_AP_A = 0.25
_AP_B = 0.50


# ── Solar geometry ────────────────────────────────────────────────────────────

def solar_noon_elevation(date, latitude_deg: float) -> float:
    """
    Return the solar elevation angle at solar noon (degrees) for a given date
    and latitude.  Uses a standard astronomical approximation.

    Parameters
    ----------
    date         : any object with a .timetuple() method
                   (e.g. datetime.date, pd.Timestamp)
    latitude_deg : observer latitude in decimal degrees (positive = north)

    Returns
    -------
    float — elevation in degrees (0 = horizon, 90 = directly overhead)
    """
    n   = date.timetuple().tm_yday          # day of year  1–366
    # Solar declination (approximate, accurate to ~0.3°)
    dec = math.radians(-23.45 * math.cos(2 * math.pi * (n + 10) / 365.25))
    lat = math.radians(latitude_deg)
    # Elevation at solar noon (hour angle = 0)
    elev = math.degrees(
        math.asin(math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec))
    )
    return round(elev, 2)


def _angstrom_prescott_sunshine(date, rs_mj: float, latitude_deg: float) -> float:
    """
    Estimate sunshine hours using the FAO-56 Angstrom-Prescott formula.

    ERA5's pre-computed sunshine_duration is derived from a coarse (~25 km)
    model grid and systematically overestimates sunshine in cloudy maritime
    climates.  Deriving sunshine hours from the daily shortwave radiation sum
    (Rs) relative to extraterrestrial radiation (Ra) produces values that are
    better calibrated and scale naturally with location: sunnier places still
    yield proportionally more sunshine hours.

    Formula: S = N × (Rs/Ra − a) / b,  clamped to [0, N]

    Parameters
    ----------
    date          : any object with a .timetuple() method
    rs_mj         : daily shortwave radiation sum (MJ/m²)
    latitude_deg  : observer latitude in decimal degrees

    Returns
    -------
    float — estimated sunshine hours for the day
    """
    if rs_mj is None or rs_mj <= 0:
        return 0.0

    n   = date.timetuple().tm_yday
    phi = math.radians(latitude_deg)

    # Solar declination (FAO-56 eq. 24)
    delta = 0.409 * math.sin(2 * math.pi * n / 365 - 1.39)

    # Sunset hour angle (FAO-56 eq. 25)
    cos_ws = -math.tan(phi) * math.tan(delta)
    cos_ws = max(-1.0, min(1.0, cos_ws))   # clamp for polar edge cases
    omega_s = math.acos(cos_ws)

    # Astronomical day length N (FAO-56 eq. 34)
    day_length = (24.0 / math.pi) * omega_s

    if day_length <= 0:
        return 0.0

    # Inverse relative Earth-Sun distance (FAO-56 eq. 23)
    dr = 1.0 + 0.033 * math.cos(2 * math.pi * n / 365)

    # Extraterrestrial radiation Ra (MJ/m²/day, FAO-56 eq. 21)
    gsc = 0.0820   # MJ/m²/min
    ra = (
        (24.0 * 60.0 / math.pi) * gsc * dr
        * (omega_s * math.sin(phi) * math.sin(delta)
           + math.cos(phi) * math.cos(delta) * math.sin(omega_s))
    )

    if ra <= 0:
        return 0.0

    # Angstrom-Prescott sunshine hours
    sunshine = day_length * (rs_mj / ra - _AP_A) / _AP_B
    return round(max(0.0, min(day_length, sunshine)), 2)


# ── API fetch ─────────────────────────────────────────────────────────────────

def fetch_weather(
    start_date: str,
    end_date:   str,
    lat:        float = DEFAULT_LAT,
    lon:        float = DEFAULT_LON,
    session = None,
) -> pd.DataFrame:
    """
    Fetch daily weather from the Open-Meteo archive API.

    Parameters
    ----------
    start_date : "YYYY-MM-DD" — inclusive start
    end_date   : "YYYY-MM-DD" — inclusive end
    lat, lon   : location coordinates
    session    : requests.Session or None (a plain session is created if None)

    Returns
    -------
    DataFrame with columns:
        date, temp_max, temp_min, temp_mean, sunshine_hours, solar_elevation_deg
    Rows with missing temp_mean are dropped.
    """
    import requests

    if session is None:
        session = requests.Session()

    params = {
        "latitude":   lat,
        "longitude":  lon,
        "start_date": start_date,
        "end_date":   end_date,
        "daily":      _DAILY_VARS,
        "timezone":   "Europe/London",
    }
    resp = session.get(ARCHIVE_URL, params=params, timeout=30)
    resp.raise_for_status()
    daily = resp.json().get("daily", {})

    dates = daily.get("time", [])
    rs    = daily.get("shortwave_radiation_sum", [None] * len(dates))

    df = pd.DataFrame({
        "date":     pd.to_datetime(dates),
        "temp_max": daily.get("temperature_2m_max",  [None] * len(dates)),
        "temp_min": daily.get("temperature_2m_min",  [None] * len(dates)),
        "temp_mean": daily.get("temperature_2m_mean", [None] * len(dates)),
        "shortwave_radiation_sum": rs,
    })
    df["sunshine_hours"] = df.apply(
        lambda row: _angstrom_prescott_sunshine(
            row["date"], row["shortwave_radiation_sum"], lat
        ),
        axis=1,
    )
    df["solar_elevation_deg"] = df["date"].apply(
        lambda d: solar_noon_elevation(d, lat)
    )
    return df.dropna(subset=["temp_mean"]).reset_index(drop=True)


# ── Cache-backed loader ───────────────────────────────────────────────────────

def load_or_fetch_weather(
    cache_dir:   str,
    lat:         float = DEFAULT_LAT,
    lon:         float = DEFAULT_LON,
    session=None,
    start_date:  str   = "2020-01-01",
) -> pd.DataFrame:
    """
    Return a DataFrame of daily weather, using a Parquet cache and only
    calling the API for dates not already cached.

    Parameters
    ----------
    cache_dir  : directory where Parquet files are stored
    lat, lon   : location coordinates
    session    : requests.Session or None
    start_date : "YYYY-MM-DD" — earliest date to fetch on a cold start

    Notes
    -----
    The Open-Meteo archive API finalises data ~6 days after the observation
    date, so the effective end date is today − 6 days.  Recent days not yet
    in the archive simply won't appear in the returned DataFrame; the energy
    model tolerates this via an inner join on date.
    """
    cutoff = (
        pd.Timestamp.now().normalize() - pd.Timedelta(days=_ARCHIVE_LAG_DAYS)
    )
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    if C.exists(CACHE_NAME, cache_dir):
        df     = C.load(CACHE_NAME, cache_dir)
        latest = pd.to_datetime(df["date"]).max()
        next_d = latest + pd.Timedelta(days=1)

        if next_d.normalize() <= cutoff.normalize():
            fetch_from = next_d.strftime("%Y-%m-%d")
            print(f"  Weather: refreshing {fetch_from} to {cutoff_str}...")
            new_df = fetch_weather(fetch_from, cutoff_str, lat, lon, session)
            if not new_df.empty:
                df = (
                    pd.concat([df, new_df])
                    .drop_duplicates("date")
                    .sort_values("date")
                    .reset_index(drop=True)
                )
                C.save(df, CACHE_NAME, cache_dir)
                print(f"    +{len(new_df)} new days  (total {len(df)})")
        else:
            print(f"  Weather: cache up to date ({latest.date()})")
    else:
        print(f"  Weather: initial fetch {start_date} to {cutoff_str}...")
        df = fetch_weather(start_date, cutoff_str, lat, lon, session)
        C.save(df, CACHE_NAME, cache_dir)
        print(f"    fetched {len(df)} days")

    return df


# ── Climate normals (WMO 1991–2020 standard reference period) ─────────────────

_NORMAL_START = "1991-01-01"
_NORMAL_END   = "2020-12-31"


def _normals_cache_name(lat: float, lon: float) -> str:
    """
    Cache key that encodes the location (to 3 d.p.) so a location change
    automatically triggers a re-fetch rather than returning stale data.
    """
    tag = (f"lat{lat:.3f}_lon{lon:.3f}"
           .replace(".", "d")
           .replace("-", "n"))
    return f"weather_normals_{tag}"


def fetch_climate_normals(
    lat:     float = DEFAULT_LAT,
    lon:     float = DEFAULT_LON,
    session = None,
) -> pd.DataFrame:
    """
    Fetch daily ERA5 data for the WMO 1991–2020 standard reference period and
    return a 12-row DataFrame of long-term monthly averages.

    Returns
    -------
    DataFrame with columns: month (1–12), sunshine_hours, temp_mean, temp_min, temp_max
    All values are rounded to 1 decimal place.
    """
    import requests

    if session is None:
        session = requests.Session()

    params = {
        "latitude":   lat,
        "longitude":  lon,
        "start_date": _NORMAL_START,
        "end_date":   _NORMAL_END,
        "daily":      _DAILY_VARS,
        "timezone":   "Europe/London",
    }
    # 30 years of daily data — allow more time for the larger response
    resp = session.get(ARCHIVE_URL, params=params, timeout=120)
    resp.raise_for_status()
    daily = resp.json().get("daily", {})

    dates = daily.get("time", [])
    rs    = daily.get("shortwave_radiation_sum", [None] * len(dates))

    df = pd.DataFrame({
        "date":     pd.to_datetime(dates),
        "temp_max": daily.get("temperature_2m_max",  [None] * len(dates)),
        "temp_min": daily.get("temperature_2m_min",  [None] * len(dates)),
        "temp_mean": daily.get("temperature_2m_mean", [None] * len(dates)),
        "shortwave_radiation_sum": rs,
    })
    df = df.dropna(subset=["temp_mean"])
    df["sunshine_hours"] = df.apply(
        lambda row: _angstrom_prescott_sunshine(
            row["date"], row["shortwave_radiation_sum"], lat
        ),
        axis=1,
    )
    df["month"] = df["date"].dt.month

    return (
        df.groupby("month")
        .agg(
            sunshine_hours = ("sunshine_hours", "mean"),
            temp_mean      = ("temp_mean",      "mean"),
            temp_min       = ("temp_min",       "mean"),
            temp_max       = ("temp_max",       "mean"),
        )
        .round(1)
        .reset_index()
    )


def load_or_fetch_climate_normals(
    cache_dir: str,
    lat:       float = DEFAULT_LAT,
    lon:       float = DEFAULT_LON,
    session = None,
) -> pd.DataFrame:
    """
    Return WMO 1991–2020 monthly climate normals for the given location,
    fetching from Open-Meteo and caching to Parquet on first call.

    The cache key encodes lat/lon to 3 d.p., so changing WEATHER_LAT/LON in
    env.ini automatically triggers a fresh fetch for the new location.

    Returns
    -------
    DataFrame — 12 rows, columns: month, sunshine_hours, temp_mean, temp_min, temp_max
    """
    cache_name = _normals_cache_name(lat, lon)

    if C.exists(cache_name, cache_dir):
        print(f"  Climate normals: loaded from cache "
              f"({_NORMAL_START[:4]}–{_NORMAL_END[:4]})")
        return C.load(cache_name, cache_dir)

    print(f"  Climate normals: fetching {_NORMAL_START} to{_NORMAL_END} "
          f"(WMO 1991-2020, this may take a moment)...")
    normals = fetch_climate_normals(lat, lon, session)
    C.save(normals, cache_name, cache_dir)
    print(f"  Climate normals: cached ({len(normals)} months)")
    return normals
