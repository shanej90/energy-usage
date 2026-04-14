"""
Energy-vs-weather OLS regression model.

Fits separate ordinary-least-squares linear models for electricity and gas
daily consumption against weather features.

No external ML library is required — numpy.linalg.lstsq provides everything
needed for OLS fitting, R², and prediction-interval calculation.

Model features
--------------
intercept            : constant term
hdd_15               : Heating Degree Days — max(0, HDD_BASE − temp_mean)
                       CIBSE standard base temperature 15.5 °C for UK residential.
                       Captures heating demand with correct physics: heat loss through
                       walls/roof is proportional to the indoor–outdoor temperature
                       *difference* (Newton's law of cooling), so energy use is linear
                       in HDD, not in raw temperature.  Using max(0, …) means the
                       heating term is zero on warm days, which prevents the nonsensical
                       negative gas predictions that a raw-temperature model produces.
temp_min             : daily minimum temperature (°C) — captures peak overnight
                       heating load, which HDD alone (based on daily mean) misses
sunshine_hours       : daily sunshine duration (hours)
solar_elevation_deg  : solar noon elevation angle (degrees)
month_sin            : sin(2π·month/12)  — cyclical month encoding
month_cos            : cos(2π·month/12)  — cyclical month encoding

The cyclical month terms capture residual seasonality not explained by the
physical weather variables (e.g. behavioural differences between calendar
months at the same temperature).

Output
------
The returned dict is JSON-serialisable and embedded in the dashboard HTML so
the client-side JavaScript forecast tool can run predictions without a server.

    {
      "electricity": {
          "fuel":             "electricity",
          "hdd_base":         15.5,
          "coefficients":     [β0, β1, …, β6],   # 7 values, same order as features
          "feature_names":    ["intercept", "hdd_15", …],
          "r_squared":        0.923,
          "residual_std":     2.14,               # kWh — used for prediction interval
          "n_samples":        731,
          "monthly_averages": {
              "1": {"temp_mean": 6.2, "temp_min": 3.1,
                    "sunshine_hours": 2.3, "solar_elevation_deg": 18.5,
                    "avg_kwh": 42.1},
              …
          }
      },
      "gas": { … }
    }
"""

import math

import numpy as np
import pandas as pd

# CIBSE standard base temperature for UK residential heating degree-day calculation.
# Below this threshold a building is assumed to need no mechanical heating.
HDD_BASE = 15.5

FEATURE_NAMES = [
    "intercept",
    "hdd_15",              # max(0, HDD_BASE − temp_mean)
    "temp_min",
    "sunshine_hours",
    "solar_elevation_deg",
    "month_sin",
    "month_cos",
]

_MIN_SAMPLES = 30   # refuse to fit with fewer observations


# ── Helpers ───────────────────────────────────────────────────────────────────

def _merge_daily(daily_df: pd.DataFrame, weather_df: pd.DataFrame) -> pd.DataFrame:
    """
    Inner-join daily energy aggregates with weather data on calendar date.

    The 'period' column in daily aggregates is tz-aware (Europe/London); the
    weather 'date' column is tz-naive UTC midnight.  Converting both to plain
    Python date objects before merging avoids any timezone mismatch.
    """
    energy = daily_df[["period", "consumption_kwh"]].copy()
    energy["_date"] = pd.to_datetime(energy["period"]).dt.date

    weather = weather_df.copy()
    weather["_date"] = pd.to_datetime(weather["date"]).dt.date

    merged = energy.merge(weather, on="_date", how="inner")
    merged["month"] = pd.to_datetime(merged["_date"]).dt.month
    return merged.dropna(subset=["consumption_kwh", "temp_mean", "sunshine_hours"])


def _feature_matrix(df: pd.DataFrame) -> np.ndarray:
    """Build the (n × 7) design matrix from a merged daily DataFrame."""
    hdd       = np.maximum(0, HDD_BASE - df["temp_mean"].values)
    month     = df["month"].values
    month_sin = np.sin(2 * math.pi * month / 12)
    month_cos = np.cos(2 * math.pi * month / 12)
    return np.column_stack([
        np.ones(len(df)),
        hdd,
        df["temp_min"].values,
        df["sunshine_hours"].fillna(0).values,
        df["solar_elevation_deg"].values,
        month_sin,
        month_cos,
    ])


# ── Model fitting ─────────────────────────────────────────────────────────────

def fit_model(
    daily_energy_df: pd.DataFrame,
    weather_df:      pd.DataFrame,
    fuel:            str,
):
    """
    Fit an OLS model for a single fuel type and return a serialisable dict.

    Parameters
    ----------
    daily_energy_df : output of T.aggregate(df, 'day') — must contain
                      columns 'period' and 'consumption_kwh'
    weather_df      : output of fetch_weather.load_or_fetch_weather()
    fuel            : "electricity" or "gas" (used only for labelling)

    Returns
    -------
    dict ready for JSON serialisation, or None if insufficient data.
    """
    merged = _merge_daily(daily_energy_df, weather_df)
    n      = len(merged)

    if n < _MIN_SAMPLES:
        print(f"  {fuel}: only {n} matched days — skipping model")
        return None

    X = _feature_matrix(merged)
    y = merged["consumption_kwh"].values

    # OLS via QR decomposition (rcond=None uses machine-precision threshold)
    coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond = None)

    y_pred    = X @ coeffs
    ss_res    = float(np.sum((y - y_pred) ** 2))
    ss_tot    = float(np.sum((y - y.mean()) ** 2))
    r2        = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    p         = X.shape[1]
    resid_std = math.sqrt(ss_res / (n - p)) if n > p else math.sqrt(ss_res / n)

    # Monthly historical averages — pre-filled into the forecast tool UI
    monthly = (
        merged.groupby("month")
        .agg(
            temp_mean            = ("temp_mean",            "mean"),
            temp_min             = ("temp_min",             "mean"),
            sunshine_hours       = ("sunshine_hours",       "mean"),
            solar_elevation_deg  = ("solar_elevation_deg",  "mean"),
            avg_kwh              = ("consumption_kwh",      "mean"),
        )
        .round(2)
    )
    monthly_avgs = {
        str(m): {
            "temp_mean":           round(float(row["temp_mean"]),           1),
            "temp_min":            round(float(row["temp_min"]),            1),
            "sunshine_hours":      round(float(row["sunshine_hours"]),      1),
            "solar_elevation_deg": round(float(row["solar_elevation_deg"]), 1),
            "avg_kwh":             round(float(row["avg_kwh"]),             2),
        }
        for m, row in monthly.iterrows()
    }

    print(f"  {fuel}: n={n}  R2={r2:.3f}  std={resid_std:.2f} kWh")

    return {
        "fuel":             fuel,
        "hdd_base":         HDD_BASE,
        "coefficients":     [round(float(c), 6) for c in coeffs],
        "feature_names":    FEATURE_NAMES,
        "r_squared":        round(r2, 4),
        "residual_std":     round(resid_std, 4),
        "n_samples":        n,
        "monthly_averages": monthly_avgs,
    }


def build_models(
    elec_daily: pd.DataFrame,
    gas_daily:  pd.DataFrame,
    weather_df: pd.DataFrame,
) -> dict:
    """
    Fit models for both fuels and return a dict ready for JSON embedding.

    Parameters
    ----------
    elec_daily : T.aggregate(elec_df, 'day')
    gas_daily  : T.aggregate(gas_df,  'day')
    weather_df : load_or_fetch_weather(...)

    Returns
    -------
    {"electricity": model_dict_or_None, "gas": model_dict_or_None}
    """
    return {
        "electricity": fit_model(elec_daily, weather_df, "electricity"),
        "gas":         fit_model(gas_daily,  weather_df, "gas"),
    }
