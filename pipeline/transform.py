"""
DataFrame construction, cost enrichment, and aggregation helpers.

Flow
----
  raw API records
      └─► consumption_to_df()   →  tidy half-hourly DataFrame
              └─► add_costs()   →  adds unit-rate and standing-charge columns
                      └─► aggregate()     →  sums to day / week / month / year
                          daily_profile() →  average usage by hour-of-day
                          heatmap_data()  →  usage intensity by (dow, hour)
"""

import datetime
from typing import Optional

import pandas as pd

# UK gas energy conversion: cubic metres → kWh
# The exact calorific value appears on your gas bill; 11.1 is a typical UK value.
GAS_M3_TO_KWH = 11.1

# Half-hourly slots per day — used to pro-rate daily standing charges
SLOTS_PER_DAY = 48

# London timezone — all local-time operations use this
_TZ_LONDON = "Europe/London"


# ---------------------------------------------------------------------------
# Raw → tidy DataFrame
# ---------------------------------------------------------------------------

def consumption_to_df(
    records: list,
    fuel: str,
    gas_is_m3: Optional[bool] = None,
) -> pd.DataFrame:
    """
    Convert raw API consumption records to a tidy half-hourly DataFrame.

    Parameters
    ----------
    records    : List of dicts from fetch.fetch_consumption()
    fuel       : 'electricity' or 'gas'
    gas_is_m3  : For gas meters — True if the meter reports in m³ (SMETS2),
                 False if already in kWh (SMETS1).  None uses a heuristic:
                 if the median reading is < 1.0 it is assumed to be m³.

    Returns
    -------
    DataFrame with columns:
        interval_start  (datetime, UTC, tz-aware)
        interval_end    (datetime, UTC, tz-aware)
        consumption     (raw value as returned by the API)
        consumption_kwh (energy in kWh — converted from m³ for SMETS2 gas)
    """
    df = pd.DataFrame(records)
    if df.empty:
        return df

    # Drop rows where the meter reported no reading (API returns null consumption)
    df = df.dropna(subset = ["consumption"])
    if df.empty:
        return df

    df["interval_start"] = pd.to_datetime(df["interval_start"], utc = True)
    df["interval_end"]   = pd.to_datetime(df["interval_end"],   utc = True)
    df = df.sort_values("interval_start").reset_index(drop = True)

    if fuel == "gas":
        if gas_is_m3 is None:
            gas_is_m3 = df["consumption"].median() < 1.0
        df["consumption_kwh"] = (
            df["consumption"] * GAS_M3_TO_KWH if gas_is_m3 else df["consumption"]
        )
    else:
        df["consumption_kwh"] = df["consumption"]

    return df


def rates_to_df(records: list) -> pd.DataFrame:
    """
    Convert unit-rate or standing-charge records to a clean DataFrame.

    Fills null valid_to (meaning "currently active") with a far-future timestamp
    so merge_asof / time-range joins behave correctly.
    """
    df = pd.DataFrame(records)
    if df.empty:
        return df

    df["valid_from"] = pd.to_datetime(df["valid_from"], utc = True)
    df["valid_to"]   = pd.to_datetime(df["valid_to"],   utc = True, errors = "coerce")
    df["valid_to"]   = df["valid_to"].fillna(pd.Timestamp("2100-01-01", tz = "UTC"))

    return df.sort_values("valid_from").reset_index(drop = True)


# ---------------------------------------------------------------------------
# Cost enrichment
# ---------------------------------------------------------------------------

def add_costs(
    consumption_df: pd.DataFrame,
    unit_rates_df: pd.DataFrame,
    standing_charges_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Join tariff rates and standing charges onto consumption data.

    Uses pandas merge_asof to match each 30-minute interval to the tariff rate
    that was active at the start of that interval.  This correctly handles both
    fixed-rate and time-of-use (e.g. Agile) tariffs.

    New columns added
    -----------------
    unit_rate_p_per_kwh      — p/kWh rate active during this slot
    cost_p / cost_gbp        — energy cost for this slot (consumption × rate)
    sc_p_per_day             — daily standing charge active at this time
    sc_slot_p / sc_slot_gbp  — standing charge pro-rated to this 30-min slot
                               (divide by 48 slots/day)
    total_cost_gbp           — cost_gbp + sc_slot_gbp

    All prices are inclusive of VAT.
    """
    df = consumption_df.copy().sort_values("interval_start")

    # --- Unit rates ---
    rates = (
        unit_rates_df.sort_values("valid_from")[["valid_from", "value_inc_vat"]]
        .rename(columns = {"value_inc_vat": "unit_rate_p_per_kwh"})
    )
    df = pd.merge_asof(df, rates, left_on = "interval_start", right_on = "valid_from")
    df["cost_p"]   = df["consumption_kwh"] * df["unit_rate_p_per_kwh"]
    df["cost_gbp"] = df["cost_p"] / 100

    # --- Standing charges ---
    sc = (
        standing_charges_df.sort_values("valid_from")[["valid_from", "value_inc_vat"]]
        .rename(columns = {"value_inc_vat": "sc_p_per_day"})
    )
    df = pd.merge_asof(df, sc, left_on = "interval_start", right_on = "valid_from")
    df["sc_slot_p"]     = df["sc_p_per_day"] / SLOTS_PER_DAY
    df["sc_slot_gbp"]   = df["sc_slot_p"] / 100
    df["total_cost_gbp"] = df["cost_gbp"] + df["sc_slot_gbp"]

    return df


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

_PERIOD_FREQS = {
    "halfhour": None,   # no flooring needed
    "hour": "h",
    "day": "D",
    "week": "W",        # handled separately (to_period)
    "month": "M",       # handled separately (to_period)
    "year": "Y",        # handled separately (to_period)
}

VALID_PERIODS = list(_PERIOD_FREQS.keys())


def _period_label(local_series: pd.Series, period: str) -> pd.Series:
    """Return a Series of period-start timestamps in London local time."""
    if period == "halfhour":
        return local_series
    if period == "hour":
        return local_series.dt.floor("h")
    if period == "day":
        return local_series.dt.normalize()

    # For week/month/year: extract plain calendar dates from the already-localised
    # series, then compute the period start as a date (no timestamp arithmetic).
    # This avoids to_period() which drops timezone info and mishandles DST
    # transitions (e.g. the 25-hour day when clocks go back in October).
    local_date = local_series.dt.date   # Python date objects, correct in local tz

    if period == "week":
        dates = [d - datetime.timedelta(days = d.weekday()) for d in local_date]
    elif period == "month":
        dates = [d.replace(day = 1) for d in local_date]
    else:  # year
        dates = [d.replace(month = 1, day = 1) for d in local_date]

    # Wrap in a Series (preserving original index) so .dt accessor is available.
    # pd.to_datetime(list) returns a DatetimeIndex which lacks .dt.
    starts = pd.Series(pd.to_datetime(dates), index = local_series.index)

    # Localise midnight dates in London time (tz_localize handles BST/GMT correctly)
    return starts.dt.tz_localize(
        _TZ_LONDON, ambiguous = "infer", nonexistent = "shift_forward"
    )


def aggregate(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """
    Aggregate a consumption DataFrame to a coarser time period.

    Parameters
    ----------
    df     : DataFrame from consumption_to_df() or add_costs()
    period : One of 'halfhour', 'hour', 'day', 'week', 'month', 'year'

    Returns a DataFrame with one row per period.  All numeric consumption and
    cost columns are summed.  The 'period' column holds the period-start time
    in London local time (tz-aware).
    """
    if period not in VALID_PERIODS:
        raise ValueError(f"period must be one of {VALID_PERIODS}, got {period!r}")

    df = df.copy()
    local = df["interval_start"].dt.tz_convert(_TZ_LONDON)
    df["period"] = _period_label(local, period)
    df["_local_date"] = local.dt.normalize()

    # Sum energy columns only — standing charge is handled separately below.
    sum_cols = ["consumption_kwh"]
    for col in ("cost_p", "cost_gbp"):
        if col in df.columns:
            sum_cols.append(col)

    result = df.groupby("period")[sum_cols].sum().reset_index()

    # Standing charges: one full day's charge per distinct calendar day in the
    # period, regardless of how many half-hourly slots are present in the data.
    # Summing sc_slot_gbp would under-charge any day with missing slots.
    if "sc_p_per_day" in df.columns:
        daily_sc = (
            df.groupby(["period", "_local_date"])["sc_p_per_day"]
            .first()
            .reset_index()
        )
        period_sc = daily_sc.groupby("period")["sc_p_per_day"].sum().reset_index()
        period_sc["sc_slot_p"]   = period_sc["sc_p_per_day"]
        period_sc["sc_slot_gbp"] = period_sc["sc_slot_p"] / 100
        result = result.merge(
            period_sc[["period", "sc_slot_p", "sc_slot_gbp"]],
            on = "period", how = "left",
        )
        result["total_cost_gbp"] = result["cost_gbp"] + result["sc_slot_gbp"]

    return result


# ---------------------------------------------------------------------------
# Pattern analysis helpers
# ---------------------------------------------------------------------------

def daily_profile(df: pd.DataFrame, metric: str = "consumption_kwh") -> pd.DataFrame:
    """
    Compute the average consumption for each hour of the day (0–23).

    Averages are first computed per (date, hour) — so a day with 2 slots per
    hour is aggregated before the cross-day mean — giving a true hourly average.

    Returns a DataFrame with columns: hour (0–23), avg_{metric}.
    """
    df = df.copy()
    local = df["interval_start"].dt.tz_convert(_TZ_LONDON)
    df["date"] = local.dt.normalize()
    df["hour"] = local.dt.hour

    # Sum within each (date, hour), then average across dates
    hourly  = df.groupby(["date", "hour"])[metric].sum().reset_index()
    profile = hourly.groupby("hour")[metric].mean().reset_index()
    profile.rename(columns = {metric: f"avg_{metric}"}, inplace = True)
    return profile


def heatmap_data(df: pd.DataFrame, metric: str = "consumption_kwh") -> pd.DataFrame:
    """
    Compute average hourly consumption by (day-of-week, hour-of-day).

    Half-hour slots are first summed to hourly totals within each calendar date,
    then averaged across all dates.  The result represents average kWh per hour,
    not per 30-minute slot.

    Returns a DataFrame with shape (7, 25) where:
      - rows are days of week (0=Mon … 6=Sun)
      - columns are 'dow' + hours 0–23
    Suitable for use with plotly.graph_objects.Heatmap.
    """
    df = df.copy()
    local = df["interval_start"].dt.tz_convert(_TZ_LONDON)
    df["date"] = local.dt.normalize()
    df["dow"]  = local.dt.dayofweek   # 0=Mon, 6=Sun
    df["hour"] = local.dt.hour

    # Sum both 30-min slots within each (date, dow, hour) → hourly totals per day
    hourly = df.groupby(["date", "dow", "hour"])[metric].sum().reset_index()

    # Average those hourly totals across all dates for each (dow, hour) cell
    pivot = (
        hourly.groupby(["dow", "hour"])[metric]
        .mean()
        .unstack(level = "hour")
        .reset_index()
    )
    return pivot



# ---------------------------------------------------------------------------
# Tariff helpers
# ---------------------------------------------------------------------------

def current_rate(rates_df: pd.DataFrame) -> Optional[pd.Series]:
    """
    Return the row from a rates DataFrame that is currently active.

    'Currently active' means valid_from <= now and (valid_to > now or valid_to
    is the far-future sentinel set by rates_to_df).

    Falls back to the most recent past rate when no exactly-active row is found
    (handles stale Agile caches where the last cached slot has already expired).

    Returns None if rates_df is empty or has no rows before now.
    """
    if rates_df.empty or "valid_from" not in rates_df.columns:
        return None
    now = pd.Timestamp.now(tz = "UTC")
    active = rates_df[
        (rates_df["valid_from"] <= now) & (rates_df["valid_to"] > now)
    ].sort_values("valid_from")
    if not active.empty:
        return active.iloc[-1]
    # Fallback: most recent rate whose valid_from is in the past
    past = rates_df[rates_df["valid_from"] <= now].sort_values("valid_from")
    return past.iloc[-1] if not past.empty else None


def tariff_summary(unit_rates_df: pd.DataFrame, sc_df: pd.DataFrame) -> dict:
    """
    Return a plain dict of current tariff headline figures.

    Keys
    ----
    unit_rate_p_inc_vat      — current unit rate in p/kWh (incl. VAT)
    unit_rate_p_exc_vat      — current unit rate in p/kWh (excl. VAT)
    sc_p_per_day_inc_vat     — current standing charge in p/day (incl. VAT)
    sc_p_per_day_exc_vat     — current standing charge in p/day (excl. VAT)
    sc_gbp_per_day_inc_vat   — standing charge in £/day
    annual_sc_gbp            — standing charge annualised to £/year

    Values are None when data is unavailable.
    """
    ur = current_rate(unit_rates_df)
    sc = current_rate(sc_df)

    sc_p   = sc["value_inc_vat"] if sc is not None else None
    sc_gbp = sc_p / 100 if sc_p is not None else None

    return {
        "unit_rate_p_inc_vat":    ur["value_inc_vat"] if ur is not None else None,
        "unit_rate_p_exc_vat":    ur["value_exc_vat"] if ur is not None else None,
        "sc_p_per_day_inc_vat":   sc_p,
        "sc_p_per_day_exc_vat":   sc["value_exc_vat"] if sc is not None else None,
        "sc_gbp_per_day_inc_vat": sc_gbp,
        "annual_sc_gbp":          round(sc_gbp * 365, 2) if sc_gbp is not None else None,
    }


def rate_history(rates_df: pd.DataFrame, sc_df: pd.DataFrame) -> pd.DataFrame:
    """
    Combine unit rates and standing charges into a single tidy history DataFrame
    suitable for a step chart.

    For Agile / time-of-use tariffs (many rows per day) this returns the full
    granularity.  For fixed tariffs (one row per price change) it returns a
    sparse history.

    Columns: valid_from, unit_rate_p, sc_p_per_day
    """
    ur_cols = rates_df[["valid_from", "value_inc_vat"]].rename(
        columns = {"value_inc_vat": "unit_rate_p"}
    )
    sc_cols = sc_df[["valid_from", "value_inc_vat"]].rename(
        columns = {"value_inc_vat": "sc_p_per_day"}
    )
    merged = pd.merge_asof(
        ur_cols.sort_values("valid_from"),
        sc_cols.sort_values("valid_from"),
        on = "valid_from",
    )
    return merged
