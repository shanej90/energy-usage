"""
Standalone dashboard builder.

Designed for headless / CI execution (no Jupyter required).  Reads credentials
from env.ini (locally) or environment variables (GitHub Actions), builds all
Plotly figures, and writes outputs/dashboard.html.

Usage
-----
    python dashboard/build_dashboard.py

Environment variables (override env.ini values — used in CI):
    OCTOPUS_API_KEY, ELECTRICITY_MPAN, ELECTRICITY_SERIAL,
    GAS_MPRN, GAS_SERIAL, ELECTRICITY_TARIFF_CODE, GAS_TARIFF_CODE,
    WEATHER_LAT, WEATHER_LON, WEATHER_LOCATION
"""

import os
import sys

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from utils.auth import build_session, get_config
from utils import cache
from pipeline import fetch as F
from pipeline import transform as T
from weather.fetch_weather import (load_or_fetch_weather,
                                    load_or_fetch_climate_normals,
                                    DEFAULT_LAT, DEFAULT_LON, DEFAULT_LOCATION)
from models.energy_model import build_models as build_energy_models

# ── Configuration ─────────────────────────────────────────────────────────────
config = get_config(PROJECT_ROOT)
cfg    = config["default"]

def _env(key):
    """Environment variable takes priority over env.ini (enables CI override)."""
    return os.environ.get(key) or cfg.get(key, "")

API_KEY     = _env("OCTOPUS_API_KEY")
ELEC_MPAN   = _env("ELECTRICITY_MPAN")
ELEC_SERIAL = _env("ELECTRICITY_SERIAL")
GAS_MPRN    = _env("GAS_MPRN")
GAS_SERIAL  = _env("GAS_SERIAL")

GAS_IS_M3  = None   # auto-detect; set True/False to override
CACHE_DIR  = os.path.join(PROJECT_ROOT, "data", "cache")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs")

# Weather location — must be set in env.ini (or as environment variables in CI).
_lat  = _env("WEATHER_LAT")
_lon  = _env("WEATHER_LON")
_loc  = _env("WEATHER_LOCATION")
if not (_lat and _lon and _loc):
    raise SystemExit(
        "Missing weather location config.  Add WEATHER_LAT, WEATHER_LON, and "
        "WEATHER_LOCATION to env.ini (or as repository secrets for CI)."
    )
WEATHER_LAT      = float(_lat)
WEATHER_LON      = float(_lon)
WEATHER_LOCATION = _loc

# Data cutoff: include up to and including the penultimate full day before today.
# Running on 12 Apr → cutoff = 11 Apr 00:00 UTC → last slot is 10 Apr 23:30 UTC.
DATA_CUTOFF = pd.Timestamp.now(tz = "UTC").normalize() - pd.Timedelta(days = 1)

# ── Chart constants ───────────────────────────────────────────────────────────
PERIODS        = ["day", "week", "month", "year"]
PERIOD_LABELS  = ["Daily", "Weekly", "Monthly", "Yearly"]
DEFAULT_PERIOD = "month"

# ── Session ───────────────────────────────────────────────────────────────────
print("Building session…")
session = build_session()

# ── Consumption data ──────────────────────────────────────────────────────────

def _load_or_fetch(fuel, mpxn, serial, cache_name):
    if cache.exists(cache_name, CACHE_DIR):
        print(f"  {fuel}: loading from cache…")
        df          = cache.load(cache_name, CACHE_DIR)
        latest      = df["interval_start"].max()
        period_from = latest.strftime("%Y-%m-%dT%H:%M:%SZ")
        new_records = F.fetch_consumption(
            session, fuel, mpxn, serial, API_KEY, period_from = period_from
        )
        if new_records:
            print(f"    +{len(new_records)} new records — refreshing cache")
            build_fn = lambda r: T.consumption_to_df(
                r, fuel, gas_is_m3 = GAS_IS_M3 if fuel == "gas" else None
            )
            df = cache.refresh(df, cache_name, CACHE_DIR, new_records, build_fn)
    else:
        print(f"  {fuel}: fetching full history from API…")
        # period_from set to a distant past date — without it the Octopus API
        # defaults to returning only the last ~14 days of data.
        records = F.fetch_consumption(
            session, fuel, mpxn, serial, API_KEY,
            period_from = "2000-01-01T00:00:00Z",
        )
        df      = T.consumption_to_df(
            records, fuel, gas_is_m3 = GAS_IS_M3 if fuel == "gas" else None
        )
        cache.save(df, cache_name, CACHE_DIR)
    return df


print("Loading consumption data…")
elec_df = _load_or_fetch("electricity", ELEC_MPAN, ELEC_SERIAL, "electricity_raw")
gas_df  = _load_or_fetch("gas",         GAS_MPRN,  GAS_SERIAL,  "gas_raw")

# Apply cutoff: drop partial / not-yet-settled slots near the current date.
elec_df = elec_df[elec_df["interval_start"] < DATA_CUTOFF].copy()
gas_df  = gas_df[ gas_df["interval_start"]  < DATA_CUTOFF].copy()

print(f"  Electricity: {len(elec_df):,} slots  "
      f"({elec_df['interval_start'].min().date()} to {elec_df['interval_start'].max().date()})")
print(f"  Gas:         {len(gas_df):,} slots  "
      f"({gas_df['interval_start'].min().date()} to {gas_df['interval_start'].max().date()})")

# ── Tariff rates ──────────────────────────────────────────────────────────────

def _get_all_rates(fuel, agreements, cache_prefix):
    """
    Fetch tariff rates for each agreement in the supplied list.

    Rates are cached per tariff code so past tariffs are never re-fetched.
    Returns (agreements, unit_rates_df, sc_df).

    Standing-charge records are clipped so each tariff's valid_from never predates
    the agreement start, preventing rates from one tariff bleeding into the window
    of the previous one when merge_asof is used later.
    """
    if not agreements:
        print(f"  {fuel}: no agreements — cost charts disabled")
        return [], T.rates_to_df([]), T.rates_to_df([])

    print(f"  {fuel}: {len(agreements)} agreement(s)")
    all_ur_dfs, all_sc_dfs = [], []

    for ag in agreements:
        tc  = ag["tariff_code"]
        pc  = F.extract_product_code(tc)
        vf  = ag.get("valid_from")
        vt  = ag.get("valid_to")
        lbl = (f"{vf[:10] if vf else '?'} to {vt[:10] if vt else 'present'}")

        # Skip zero-duration agreements (valid_from == valid_to) — these are
        # transition artefacts that produce 400 errors on the rates endpoint.
        if vf and vt and vf == vt:
            print(f"    {tc}  ({lbl}): skipped (zero-duration)")
            continue

        # Cache key is derived from the tariff code — rate data for a past tariff
        # is immutable so these files never need to be invalidated.
        slug    = tc.lower().replace("-", "_")
        ur_name = f"{cache_prefix}_ur_{slug}"
        sc_name = f"{cache_prefix}_sc_{slug}"

        if cache.exists(ur_name, CACHE_DIR) and cache.exists(sc_name, CACHE_DIR):
            ur_df = cache.load(ur_name, CACHE_DIR)
            sc_df = cache.load(sc_name, CACHE_DIR)
            print(f"    {tc}  ({lbl}): {len(ur_df)} unit rates (cached)")
        else:
            print(f"    {tc}  ({lbl}): fetching…")
            try:
                ur_df = T.rates_to_df(F.fetch_unit_rates(
                    session, fuel, pc, tc, period_from = vf, period_to = vt))
                sc_df = T.rates_to_df(F.fetch_standing_charges(
                    session, fuel, pc, tc))
            except Exception as exc:
                print(f"      skipped — API error: {exc}")
                continue
            cache.save(ur_df, ur_name, CACHE_DIR)
            cache.save(sc_df, sc_name, CACHE_DIR)
            print(f"      fetched {len(ur_df)} unit rates, {len(sc_df)} standing charges")

        # Clip SC valid_from to the agreement start so that a tariff whose SC
        # rate record predates your agreement start does not bleed into the
        # previous tariff's window when merge_asof looks for the nearest rate.
        if vf and not sc_df.empty:
            ag_start = pd.Timestamp(vf, tz = "UTC")
            sc_df = sc_df.copy()
            sc_df["valid_from"] = sc_df["valid_from"].where(
                sc_df["valid_from"] >= ag_start, ag_start
            )

        all_ur_dfs.append(ur_df)
        all_sc_dfs.append(sc_df)

    def _combine(dfs):
        if not dfs:
            return T.rates_to_df([])
        return (pd.concat(dfs)
                .drop_duplicates(subset = ["valid_from"])
                .sort_values("valid_from")
                .reset_index(drop = True))

    return agreements, _combine(all_ur_dfs), _combine(all_sc_dfs)


def _current_tariff_code(agreements):
    """Return the tariff code of the currently active agreement, or None."""
    if not agreements:
        return None
    now = pd.Timestamp.now(tz = "UTC")
    for ag in reversed(agreements):  # agreements are sorted oldest-first
        vt = ag.get("valid_to")
        if vt is None or pd.Timestamp(vt, tz = "UTC") > now:
            return ag["tariff_code"]
    return agreements[-1]["tariff_code"]  # fallback: most recent


def _rate_str(rates_df):
    r = T.current_rate(rates_df)
    return f"{r['value_inc_vat']:.4f} p/kWh" if r is not None else "None"


print("Fetching account tariff history…")
try:
    _account_number           = F.fetch_account_number(session, API_KEY)
    print(f"  Account: {_account_number}")
    _elec_agmt_map, _gas_agmt_map = F.fetch_account_agreements(
        session, _account_number, API_KEY
    )
    _elec_meter_agmts = _elec_agmt_map.get(ELEC_MPAN, [])
    _gas_meter_agmts  = _gas_agmt_map.get(GAS_MPRN, [])
    print(f"  Electricity: {len(_elec_meter_agmts)} agreement(s) on MPAN {ELEC_MPAN}")
    print(f"  Gas:         {len(_gas_meter_agmts)} agreement(s) on MPRN {GAS_MPRN}")
except Exception as _e:
    print(f"  Could not fetch account agreements: {_e}")
    _elec_meter_agmts, _gas_meter_agmts = [], []

print("Fetching tariff rates…")
elec_agreements, elec_rates_df, elec_sc_df = _get_all_rates(
    "electricity", _elec_meter_agmts, "electricity"
)
gas_agreements,  gas_rates_df,  gas_sc_df  = _get_all_rates(
    "gas", _gas_meter_agmts, "gas"
)

elec_tariff_code = _current_tariff_code(elec_agreements)
gas_tariff_code  = _current_tariff_code(gas_agreements)

print(f"  Elec current tariff: {elec_tariff_code or 'N/A'}"
      f"  | current rate: {_rate_str(elec_rates_df)}")
print(f"  Gas  current tariff: {gas_tariff_code or 'N/A'}"
      f"  | current rate: {_rate_str(gas_rates_df)}")

elec_summary    = T.tariff_summary(elec_rates_df, elec_sc_df)
gas_summary     = T.tariff_summary(gas_rates_df,  gas_sc_df)
has_elec_tariff = not elec_rates_df.empty
has_gas_tariff  = not gas_rates_df.empty

if elec_summary["unit_rate_p_inc_vat"]:
    print(f"  Electricity: {elec_summary['unit_rate_p_inc_vat']:.2f}p/kWh  |  "
          f"{elec_summary['sc_p_per_day_inc_vat']:.2f}p/day standing charge")
if gas_summary["unit_rate_p_inc_vat"]:
    print(f"  Gas:         {gas_summary['unit_rate_p_inc_vat']:.2f}p/kWh  |  "
          f"{gas_summary['sc_p_per_day_inc_vat']:.2f}p/day standing charge")

# ── Cost enrichment ───────────────────────────────────────────────────────────
# Costs are always computed fresh from the raw data + cached rates.
# There is deliberately no separate "costed" cache — it would go stale whenever
# new consumption records are added via the incremental refresh.
print("Enriching with cost data…")
if not elec_rates_df.empty:
    elec_df = T.add_costs(elec_df, elec_rates_df, elec_sc_df)
if not gas_rates_df.empty:
    gas_df  = T.add_costs(gas_df,  gas_rates_df,  gas_sc_df)

has_elec_costs = "cost_gbp" in elec_df.columns
has_gas_costs  = "cost_gbp" in gas_df.columns

# ── Aggregations ──────────────────────────────────────────────────────────────
print("Computing aggregations…")

DATA_YEARS = sorted(set(
    elec_df["interval_start"].dt.year.tolist() +
    gas_df["interval_start"].dt.year.tolist()
))

elec_aggs = {p: T.aggregate(elec_df, p) for p in PERIODS}
gas_aggs  = {p: T.aggregate(gas_df,  p) for p in PERIODS}

# ── Weather data & energy models ──────────────────────────────────────────────
print("Fetching weather data…")
try:
    _energy_start = min(
        elec_df["interval_start"].min(),
        gas_df["interval_start"].min(),
    ).strftime("%Y-%m-%d")
    weather_df = load_or_fetch_weather(
        CACHE_DIR, lat = WEATHER_LAT, lon = WEATHER_LON,
        session = session, start_date = _energy_start,
    )
    print(f"  {len(weather_df)} days  "
          f"({weather_df['date'].min().date()} to {weather_df['date'].max().date()})")
    print("Fitting energy-vs-weather models…")
    energy_models = build_energy_models(elec_aggs["day"], gas_aggs["day"], weather_df)
    HAS_WEATHER   = len(weather_df) >= 30
except Exception as _we:
    print(f"  Weather data unavailable: {_we}")
    weather_df    = None
    energy_models = {"electricity": None, "gas": None}
    HAS_WEATHER   = False

print("Fetching climate normals…")
try:
    climate_normals = load_or_fetch_climate_normals(
        CACHE_DIR, lat = WEATHER_LAT, lon = WEATHER_LON, session = session
    )
except Exception as _ce:
    print(f"  Climate normals unavailable: {_ce}")
    climate_normals = None

# ── Chart builders ────────────────────────────────────────────────────────────

def _period_buttons(aggs, n_per):
    """Return updatemenus button list for the period selector."""
    total = len(PERIODS) * n_per
    buttons = []
    for i, (period, label) in enumerate(zip(PERIODS, PERIOD_LABELS)):
        vis = [False] * total
        for t in range(n_per):
            vis[i * n_per + t] = True
        buttons.append(dict(label = label, method = "update", args = [{"visible": vis}]))
    return buttons


def build_timeseries_fig(aggs, fuel_label, metric, color_energy, color_sc):
    """
    Build a single-metric timeseries bar chart with a period selector.

    Parameters
    ----------
    metric : 'usage'  — consumption_kwh bars, y-axis in kWh
             'cost'   — cost_gbp (energy) + sc_slot_gbp (standing charge) stacked,
                        y-axis in £.  Returns None when cost data is absent.
    """
    sample    = list(aggs.values())[0]
    has_costs = "cost_gbp" in sample.columns
    has_sc    = "sc_slot_gbp" in sample.columns

    if metric == "cost":
        if not has_costs:
            return None
        n_per = (1 if has_costs else 0) + (1 if has_sc else 0)
        title = f"{fuel_label} — Cost (£ incl. VAT)"
    else:
        n_per = 1
        title = f"{fuel_label} — Usage (kWh)"

    fig       = go.Figure()
    hover_fmt = {"day": "%d %b %Y", "week": "w/c %d %b %Y", "month": "%b %Y", "year": "%Y"}

    for i, (period, label) in enumerate(zip(PERIODS, PERIOD_LABELS)):
        df   = aggs[period]
        vis  = period == DEFAULT_PERIOD
        hfmt = hover_fmt[period]

        if metric == "usage":
            fig.add_trace(go.Bar(
                x = df["period"], y = df["consumption_kwh"],
                name = f"{label} kWh", marker_color = color_energy,
                visible = vis, showlegend = False,
                hovertemplate = f"%{{x|{hfmt}}}<br><b>%{{y:.1f}} kWh</b><extra></extra>",
            ))
        else:  # cost — stacked bars: energy cost (darker) + standing charge (lighter)
            if has_costs:
                fig.add_trace(go.Bar(
                    x = df["period"], y = df["cost_gbp"],
                    name = "Energy cost", marker_color = color_energy,
                    visible = vis,
                    legendgroup = "energy", showlegend = (i == PERIODS.index(DEFAULT_PERIOD)),
                    hovertemplate = f"%{{x|{hfmt}}}<br>Energy: <b>£%{{y:.2f}}</b><extra></extra>",
                ))
            if has_sc:
                fig.add_trace(go.Bar(
                    x = df["period"], y = df["sc_slot_gbp"],
                    name = "Standing charge", marker_color = color_sc,
                    visible = vis,
                    legendgroup = "sc", showlegend = (i == PERIODS.index(DEFAULT_PERIOD)),
                    hovertemplate = f"%{{x|{hfmt}}}<br>Standing charge: <b>£%{{y:.2f}}</b><extra></extra>",
                ))

    legend_cfg = (
        dict(orientation = "h", yanchor = "bottom", y = 1.02, xanchor = "right", x = 1)
        if metric == "cost" else None
    )
    fig.update_layout(
        title   = title,
        barmode = "stack",
        height  = 360,
        margin  = dict(t = 90, b = 60, l = 70, r = 30),
        plot_bgcolor  = "white",
        paper_bgcolor = "white",
        legend = legend_cfg,
        updatemenus = [dict(
            type = "buttons", direction = "right",
            x = 0.0, xanchor = "left", y = 1.16, yanchor = "top",
            buttons = _period_buttons(aggs, n_per),
            active = PERIODS.index(DEFAULT_PERIOD),
            showactive = True, bgcolor = "#f5f5f5", bordercolor = "#cccccc",
            font = dict(size = 12),
        )],
    )
    fig.update_yaxes(title_text = "kWh" if metric == "usage" else "£",
                     gridcolor = "#eeeeee")
    fig.update_xaxes(
        showgrid = False,
        rangeslider = dict(visible = True, thickness = 0.04),
        type = "date",
    )
    return fig


def build_heatmap_fig(df, fuel_label, colorscale = "Blues"):
    """
    Heatmap of average kWh by (day-of-week, hour-of-day).

    Single trace seeded with all-time data.  The global filter bar updates the
    z-values via a JavaScript lookup (see _compute_heatmap_lookup / JS below).
    """
    days  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    hours = list(range(24))
    hm    = T.heatmap_data(df)
    z     = hm.set_index("dow").reindex(range(7))[hours].values

    fig = go.Figure(go.Heatmap(
        z             = z,
        x             = [f"{h:02d}:00" for h in hours],
        y             = days,
        colorscale    = colorscale,
        colorbar      = dict(title = "Avg kWh/hr", thickness = 15),
        hovertemplate = "%{y} %{x}<br>Avg <b>%{z:.3f} kWh</b><extra></extra>",
    ))
    fig.update_layout(
        title  = f"{fuel_label} — Usage Pattern (avg kWh per hour)",
        xaxis  = dict(title = "Hour", tickmode = "array",
                      tickvals = [f"{h:02d}:00" for h in range(0, 24, 2)]),
        height = 300,
        margin = dict(t = 60, b = 50, l = 60, r = 60),
        plot_bgcolor  = "white",
        paper_bgcolor = "white",
    )
    return fig


def build_profile_fig(elec_df, gas_df):
    """
    Average hourly consumption profile for both fuels.

    Two traces seeded with all-time data.  The global filter bar updates the
    y-values via a JavaScript lookup (see _compute_profile_lookup / JS below).
    """
    ep  = T.daily_profile(elec_df)
    gp  = T.daily_profile(gas_df)
    fig = go.Figure([
        go.Bar(
            x = ep["hour"], y = ep["avg_consumption_kwh"],
            name = "Electricity", marker_color = "#1f77b4",
            hovertemplate = "%{x}:00<br>Avg <b>%{y:.3f} kWh</b><extra>Electricity</extra>",
        ),
        go.Bar(
            x = gp["hour"], y = gp["avg_consumption_kwh"],
            name = "Gas", marker_color = "#ff7f0e",
            hovertemplate = "%{x}:00<br>Avg <b>%{y:.3f} kWh</b><extra>Gas</extra>",
        ),
    ])
    fig.update_layout(
        title   = "Average Consumption by Hour of Day",
        xaxis   = dict(title = "Hour of day", tickmode = "array",
                       tickvals = list(range(0, 24, 2)),
                       ticktext = [f"{h:02d}:00" for h in range(0, 24, 2)]),
        yaxis   = dict(title = "Average kWh per hour", gridcolor = "#eeeeee"),
        barmode = "group", height = 340,
        plot_bgcolor  = "white",
        paper_bgcolor = "white",
        legend  = dict(orientation = "h", yanchor = "bottom", y = 1.02,
                       xanchor = "right", x = 1),
        margin  = dict(t = 60, b = 60, l = 70, r = 30),
    )
    return fig


# ---------------------------------------------------------------------------
# Pattern-chart data lookup builders
# (called at build time; results are embedded as JSON in the dashboard HTML
#  so the JS filter can update heatmap / profile traces without a page reload)
# ---------------------------------------------------------------------------

def _compute_heatmap_lookup(df, data_years):
    """
    Return {year_key: {month_key: z_matrix}} for all (year, month) combinations.

    year_key  : "all" or str(year int)
    month_key : "0" (all months) or "1"…"12"
    z_matrix  : list[list[float|None]] shape (7, 24) — None where no data
    """
    hours = list(range(24))

    def _z(subset):
        if subset.empty:
            return None
        n   = subset["interval_start"].dt.date.nunique()
        hm  = T.heatmap_data(subset)
        mat = hm.set_index("dow").reindex(range(7))[hours]
        z   = [
            [None if pd.isna(v) else round(float(v), 4) for v in row]
            for row in mat.values.tolist()
        ]
        return {"z": z, "n": n}

    result = {}
    for yk in ["all"] + [str(y) for y in data_years]:
        base = df if yk == "all" else df[df["interval_start"].dt.year == int(yk)]
        result[yk] = {"0": _z(base)}
        if not base.empty:
            local = base["interval_start"].dt.tz_convert("Europe/London")
            for m in range(1, 13):
                result[yk][str(m)] = _z(base[local.dt.month == m])
        else:
            for m in range(1, 13):
                result[yk][str(m)] = None
    return result


def _compute_profile_lookup(elec_df, gas_df, data_years):
    """
    Return {year_key: {month_key: {"elec": [24 floats], "gas": [24 floats]}}}

    None entries indicate no data for that combination.
    """
    def _p(subset):
        if subset.empty:
            return None
        n    = subset["interval_start"].dt.date.nunique()
        prof = T.daily_profile(subset).set_index("hour").reindex(range(24))
        y    = [
            None if pd.isna(v) else round(float(v), 4)
            for v in prof["avg_consumption_kwh"].tolist()
        ]
        return {"y": y, "n": n}

    result = {}
    for yk in ["all"] + [str(y) for y in data_years]:
        if yk == "all":
            ed, gd = elec_df, gas_df
        else:
            yr = int(yk)
            ed = elec_df[elec_df["interval_start"].dt.year == yr]
            gd = gas_df[ gas_df["interval_start"].dt.year  == yr]
        result[yk] = {"0": {"elec": _p(ed), "gas": _p(gd)}}
        if not ed.empty or not gd.empty:
            el = ed["interval_start"].dt.tz_convert("Europe/London") if not ed.empty else None
            gl = gd["interval_start"].dt.tz_convert("Europe/London") if not gd.empty else None
            for m in range(1, 13):
                em = ed[el.dt.month == m] if el is not None else ed.iloc[0:0]
                gm = gd[gl.dt.month == m] if gl is not None else gd.iloc[0:0]
                result[yk][str(m)] = {"elec": _p(em), "gas": _p(gm)}
        else:
            for m in range(1, 13):
                result[yk][str(m)] = {"elec": None, "gas": None}
    return result


def build_rates_summary_fig(elec_summary, gas_summary, elec_tariff_code, gas_tariff_code):
    def fp(v): return f"{v:.2f}p" if v is not None else "N/A"
    def fg(v): return f"\u00a3{v:.2f}" if v is not None else "N/A"
    label_col = [
        "<b>Tariff code</b>",
        "Unit rate (p/kWh, incl. VAT)", "Unit rate (p/kWh, excl. VAT)",
        "Standing charge (p/day, incl. VAT)", "Standing charge (\u00a3/day, incl. VAT)",
        "Standing charge annualised (\u00a3/year est.)",
    ]
    elec_col = [
        elec_tariff_code or "\u2014",
        fp(elec_summary.get("unit_rate_p_inc_vat")), fp(elec_summary.get("unit_rate_p_exc_vat")),
        fp(elec_summary.get("sc_p_per_day_inc_vat")), fg(elec_summary.get("sc_gbp_per_day_inc_vat")),
        fg(elec_summary.get("annual_sc_gbp")),
    ]
    gas_col = [
        gas_tariff_code or "\u2014",
        fp(gas_summary.get("unit_rate_p_inc_vat")), fp(gas_summary.get("unit_rate_p_exc_vat")),
        fp(gas_summary.get("sc_p_per_day_inc_vat")), fg(gas_summary.get("sc_gbp_per_day_inc_vat")),
        fg(gas_summary.get("annual_sc_gbp")),
    ]
    row_fills = ["#f4f6f8" if i % 2 == 0 else "white" for i in range(len(label_col))]
    fig = go.Figure(go.Table(
        columnwidth = [3, 1.5, 1.5],
        header = dict(
            values     = ["<b></b>", "<b>\u26a1 Electricity</b>", "<b>\U0001f525 Gas</b>"],
            fill_color = "#2c3e50", font = dict(color = "white", size = 13),
            align = "left", height = 36,
        ),
        cells = dict(
            values     = [label_col, elec_col, gas_col],
            fill_color = [row_fills, row_fills, row_fills],
            align      = ["left", "center", "center"],
            font = dict(size = 12), height = 30,
        ),
    ))
    fig.update_layout(title = "Current Tariff Rates", height = 280,
                      margin = dict(t = 50, b = 10, l = 10, r = 10))
    return fig


def build_tariff_fig(unit_rates_df, sc_df, fuel_label, color, fill_color):
    sentinel = pd.Timestamp("2099-01-01", tz = "UTC")
    ur       = unit_rates_df[unit_rates_df["valid_from"] < sentinel].copy()
    sc       = sc_df[sc_df["valid_from"] < sentinel].copy()
    summary  = T.tariff_summary(unit_rates_df, sc_df)
    fig = make_subplots(
        rows = 2, cols = 1, shared_xaxes = True,
        subplot_titles = [f"{fuel_label} \u2014 Unit Rate (p/kWh incl. VAT)",
                          f"{fuel_label} \u2014 Standing Charge (p/day incl. VAT)"],
        vertical_spacing = 0.14,
    )
    fig.add_trace(go.Scatter(
        x = ur["valid_from"], y = ur["value_inc_vat"], mode = "lines",
        line = dict(shape = "hv", color = color, width = 1.5),
        fill = "tozeroy", fillcolor = fill_color,
        name = "Unit rate", showlegend = False,
        hovertemplate = "%{x|%d %b %Y %H:%M}<br><b>%{y:.2f}p/kWh</b><extra></extra>",
    ), row = 1, col = 1)
    if summary["unit_rate_p_inc_vat"] is not None:
        fig.add_hline(y = summary["unit_rate_p_inc_vat"],
                      line_dash = "dash", line_color = "#555555", line_width = 1,
                      annotation_text = f"Now: {summary['unit_rate_p_inc_vat']:.2f}p/kWh",
                      annotation_position = "top right", annotation_font_size = 10,
                      row = 1, col = 1)
    fig.add_trace(go.Scatter(
        x = sc["valid_from"], y = sc["value_inc_vat"], mode = "lines",
        line = dict(shape = "hv", color = color, width = 2),
        fill = "tozeroy", fillcolor = fill_color,
        name = "Standing charge", showlegend = False,
        hovertemplate = "%{x|%d %b %Y}<br><b>%{y:.2f}p/day</b><extra></extra>",
    ), row = 2, col = 1)
    if summary["sc_p_per_day_inc_vat"] is not None:
        fig.add_hline(y = summary["sc_p_per_day_inc_vat"],
                      line_dash = "dash", line_color = "#555555", line_width = 1,
                      annotation_text = f"Now: {summary['sc_p_per_day_inc_vat']:.2f}p/day",
                      annotation_position = "top right", annotation_font_size = 10,
                      row = 2, col = 1)
    fig.update_layout(
        title = f"{fuel_label} \u2014 Tariff Rate History",
        height = 420, plot_bgcolor = "white", paper_bgcolor = "white",
        margin = dict(t = 70, b = 40, l = 70, r = 130),
    )
    fig.update_yaxes(title_text = "p/kWh", gridcolor = "#eeeeee", row = 1, col = 1)
    fig.update_yaxes(title_text = "p/day",  gridcolor = "#eeeeee", row = 2, col = 1)
    fig.update_xaxes(showgrid = False)
    return fig


# ── Weather scatter chart builders ───────────────────────────────────────────

_MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Fuel colours — consistent with the rest of the dashboard.
_ELEC_COLOUR = "#1f77b4"
_GAS_COLOUR  = "#d62728"


def _merge_daily_weather(daily_df, wdf):
    """Inner-join daily energy and weather DataFrames on calendar date."""
    e = daily_df[["period", "consumption_kwh"]].copy()
    e["_date"] = pd.to_datetime(e["period"]).dt.date
    w = wdf.copy()
    w["_date"] = pd.to_datetime(w["date"]).dt.date
    merged = e.merge(w, on = "_date", how = "inner")
    merged["month"] = pd.to_datetime(merged["_date"]).dt.month
    return merged.dropna(subset = ["consumption_kwh", "temp_mean"])


def build_temp_scatter_fig(elec_daily, gas_daily, wdf, location_name):
    """
    Two-panel scatter: daily kWh vs mean temperature (electricity | gas).
    """
    em = _merge_daily_weather(elec_daily, wdf)
    gm = _merge_daily_weather(gas_daily,  wdf)

    fig = make_subplots(
        rows = 1, cols = 2,
        subplot_titles = [
            "Electricity — kWh vs Mean Temperature",
            "Gas — kWh vs Mean Temperature",
        ],
        horizontal_spacing = 0.10,
    )

    def _scatter(df, colour):
        return go.Scatter(
            x    = df["temp_mean"],
            y    = df["consumption_kwh"],
            mode = "markers",
            marker = dict(color = colour, size = 5, opacity = 0.55,
                          line = dict(width = 0)),
            customdata   = df[["_date", "temp_min", "temp_max",
                                "sunshine_hours"]].values,
            hovertemplate = (
                "<b>%{customdata[0]}</b><br>"
                "Mean temp: %{x:.1f} °C  "
                "(min %{customdata[1]:.1f} / max %{customdata[2]:.1f})<br>"
                "Sunshine: %{customdata[3]:.1f} h<br>"
                "Consumption: <b>%{y:.1f} kWh</b><extra></extra>"
            ),
            showlegend = False,
        )

    fig.add_trace(_scatter(em, _ELEC_COLOUR), row = 1, col = 1)
    fig.add_trace(_scatter(gm, _GAS_COLOUR),  row = 1, col = 2)

    fig.update_layout(
        title  = f"Daily Consumption vs Temperature — {location_name}",
        height = 400,
        plot_bgcolor  = "white",
        paper_bgcolor = "white",
        margin = dict(t = 80, b = 60, l = 70, r = 30),
    )
    fig.update_xaxes(title_text = "Mean temperature (°C)", showgrid = False)
    fig.update_yaxes(title_text = "Daily kWh", gridcolor = "#eeeeee")
    return fig


def build_sunshine_scatter_fig(elec_daily, gas_daily, wdf):
    """
    Two-panel scatter: daily kWh vs sunshine hours (electricity | gas).
    """
    em = _merge_daily_weather(elec_daily, wdf)
    gm = _merge_daily_weather(gas_daily,  wdf)

    fig = make_subplots(
        rows = 1, cols = 2,
        subplot_titles = [
            "Electricity — kWh vs Sunshine Hours",
            "Gas — kWh vs Sunshine Hours",
        ],
        horizontal_spacing = 0.10,
    )

    def _scatter(df, colour):
        return go.Scatter(
            x    = df["sunshine_hours"],
            y    = df["consumption_kwh"],
            mode = "markers",
            marker = dict(color = colour, size = 5, opacity = 0.55,
                          line = dict(width = 0)),
            customdata   = df[["_date", "temp_mean",
                                "solar_elevation_deg"]].values,
            hovertemplate = (
                "<b>%{customdata[0]}</b><br>"
                "Sunshine: %{x:.1f} h  |  "
                "Mean temp: %{customdata[1]:.1f} °C  |  "
                "Solar elev: %{customdata[2]:.1f}°<br>"
                "Consumption: <b>%{y:.1f} kWh</b><extra></extra>"
            ),
            showlegend = False,
        )

    fig.add_trace(_scatter(em, _ELEC_COLOUR), row = 1, col = 1)
    fig.add_trace(_scatter(gm, _GAS_COLOUR),  row = 1, col = 2)

    fig.update_layout(
        title  = "Daily Consumption vs Sunshine Hours",
        height = 400,
        plot_bgcolor  = "white",
        paper_bgcolor = "white",
        margin = dict(t = 80, b = 60, l = 70, r = 30),
    )
    fig.update_xaxes(title_text = "Sunshine hours per day", showgrid = False)
    fig.update_yaxes(title_text = "Daily kWh", gridcolor = "#eeeeee")
    return fig


def build_forecast_tool_html(models, location_name, climate_normals=None):
    """
    Return a self-contained HTML+JS block for the interactive forecast tool.

    The tool pre-fills historical monthly weather averages for the selected
    month and lets users override individual values before predicting expected
    daily kWh (electricity and gas) with an approximate 95 % prediction interval
    (±2σ of in-sample residuals).

    No server required — prediction runs entirely in the browser using the
    OLS coefficients embedded as JSON.
    """
    import json as _json
    import datetime as _dt

    elec_m = models.get("electricity")
    gas_m  = models.get("gas")

    if elec_m is None and gas_m is None:
        return (
            "<p style='color:#888'>Insufficient weather-matched data to fit "
            "a consumption model.</p>"
        )

    def _r2_badge(m):
        if m is None:
            return "N/A"
        r2     = m["r_squared"]
        colour = "#27ae60" if r2 >= 0.8 else "#e67e22" if r2 >= 0.6 else "#c0392b"
        return f'<span style="color:{colour};font-weight:600">R² = {r2:.3f}</span>'

    def _n_badge(m):
        return f'{m["n_samples"]} days' if m else ""

    # Pre-compute solar elevation per month for the JS lookup (mathematical,
    # not a user input — completely determined by calendar month and latitude).
    from weather.fetch_weather import solar_noon_elevation as _sol
    solar_by_month = {
        str(m): round(_sol(_dt.date(2024, m, 15), WEATHER_LAT), 1)
        for m in range(1, 13)
    }

    # ── Equation strings (generated from fitted coefficients) ─────────────────
    def _equation_html(m, fuel_label, colour):
        if m is None:
            return f"<p style='color:#aaa'>{fuel_label}: model not available</p>"
        c  = m["coefficients"]
        r2 = m["r_squared"]
        n  = m["n_samples"]

        def _term(coeff, desc):
            sign = "+" if coeff >= 0 else "−"
            return f"  {sign} {abs(coeff):.4f} &times; {desc}"

        hdd_base = m.get("hdd_base", 15.5)
        lines = [
            f"<strong style='color:{colour}'>{fuel_label}</strong>",
            f"Daily kWh &nbsp;= &nbsp;{c[0]:.4f} &nbsp;(intercept)",
            _term(c[1], f"hdd<sub>15</sub> = max(0,&nbsp;{hdd_base}&nbsp;−&nbsp;mean temp)"),
            _term(c[2], "min temperature (°C)"),
            _term(c[3], "sunshine hours / day"),
            _term(c[4], "solar noon elevation (°)"),
            _term(c[5], "sin(2π &middot; month / 12)"),
            _term(c[6], "cos(2π &middot; month / 12)"),
            f"<span style='color:#888'>R² = {r2:.3f} &nbsp; fitted on {n} days</span>",
        ]
        return "<br>".join(lines)

    eq_elec = _equation_html(elec_m, "⚡ Electricity", _ELEC_COLOUR)
    eq_gas  = _equation_html(gas_m,  "🔥 Gas",         _GAS_COLOUR)

    # ── Helpers ────────────────────────────────────────────────────────────────
    models_json = _json.dumps({"electricity": elec_m, "gas": gas_m})
    solar_json  = _json.dumps(solar_by_month)

    # Climate normals lookup by month string — used as fallback when the model
    # has no monthly_averages entry (months with no consumption data yet).
    if climate_normals is not None and not climate_normals.empty:
        normals_by_month = {
            str(int(row["month"])): {
                "temp_mean":      round(float(row["temp_mean"]),      1),
                "temp_min":       round(float(row["temp_min"]),       1),
                "sunshine_hours": round(float(row["sunshine_hours"]), 1),
            }
            for _, row in climate_normals.iterrows()
        }
    else:
        normals_by_month = {}
    normals_weather_json = _json.dumps(normals_by_month)

    elec_r2 = _r2_badge(elec_m)
    gas_r2  = _r2_badge(gas_m)
    elec_n  = _n_badge(elec_m)
    gas_n   = _n_badge(gas_m)

    def _js_safe(s):
        return s.replace("</", "<\\/")

    # ── Climate normals reference table ───────────────────────────────────────
    def _normals_table_html():
        if climate_normals is None or climate_normals.empty:
            return ""
        rows = "".join(
            f"<tr><td>{_MONTH_LABELS[int(r['month']) - 1]}</td>"
            f"<td>{r['sunshine_hours']:.1f}</td>"
            f"<td>{r['temp_mean']:.1f}</td>"
            f"<td>{r['temp_min']:.1f}</td>"
            f"<td>{r['temp_max']:.1f}</td></tr>"
            for _, r in climate_normals.iterrows()
        )
        return f"""
<details style="margin-top:12px">
  <summary style="cursor:pointer;font-size:12px;color:#666;user-select:none">
    Reference: 30-year average conditions (1991&#x2013;2020, {location_name}) &#x25B8;
  </summary>
  <div style="overflow-x:auto;margin-top:8px">
    <table class="fc-ref-table">
      <thead><tr>
        <th>Month</th><th>Sunshine<br>(hrs/day)</th>
        <th>Mean&nbsp;temp<br>(&deg;C)</th>
        <th>Min&nbsp;temp<br>(&deg;C)</th>
        <th>Max&nbsp;temp<br>(&deg;C)</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <p style="margin:6px 0 0;font-size:10px;color:#aaa">
    Source: <a href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo.com</a>
    ERA5 reanalysis &mdash;
    <a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noopener">CC BY 4.0</a>.
    WMO 1991&#x2013;2020 standard reference period.
    Sunshine hours are estimated using the FAO-56 Angstrom-Prescott formula
    (derived from daily shortwave radiation vs. extraterrestrial radiation);
    ERA5 pre-computed sunshine duration tends to overestimate in cloudy climates.
  </p>
</details>"""

    normals_html = _normals_table_html()

    month_options = " ".join(
        f'<option value="{i+1}">{n}</option>'
        for i, n in enumerate(_MONTH_LABELS)
    )

    return f"""
<div class="forecast-tool">
  <h3 style="margin:0 0 6px 0">&#x1F4CA; Monthly Consumption Forecast — {location_name}</h3>
  <p style="margin:0 0 4px 0;color:#555;font-size:13px">
    Select a month, adjust the weather inputs if needed, then click
    <strong>Calculate</strong> to see expected daily consumption with an
    approximate 95&nbsp;% prediction interval.<br>
    Model quality: &#x26A1;&nbsp;Electricity: {elec_r2} ({elec_n})
    &nbsp;|&nbsp; &#x1F525;&nbsp;Gas: {gas_r2} ({gas_n})
  </p>

  <div class="fc-grid">
    <div class="fc-field">
      <label for="fc-month">
        Month
        <span class="fc-hint">sets weather defaults &amp; seasonal correction</span>
      </label>
      <select id="fc-month" onchange="fcMonthChange()">{month_options}</select>
    </div>
    <div class="fc-field">
      <label for="fc-temp-mean">Mean temperature (°C)</label>
      <input type="number" id="fc-temp-mean" step="0.5">
    </div>
    <div class="fc-field">
      <label for="fc-temp-min">Min temperature (°C)</label>
      <input type="number" id="fc-temp-min" step="0.5">
    </div>
    <div class="fc-field">
      <label for="fc-sunshine">Sunshine hours / day</label>
      <input type="number" id="fc-sunshine" step="0.1" min="0" max="16">
    </div>
    <div class="fc-field">
      <label for="fc-solar">
        Solar noon elevation (°)
        <span class="fc-hint">auto-calculated from month &amp; latitude</span>
      </label>
      <input type="number" id="fc-solar" step="0.1" readonly
             style="background:#f0f0f0;cursor:default">
    </div>
    <div class="fc-field">
      <label for="fc-gas-use">
        Gas used for
        <span class="fc-hint">sets non-heating baseline floor</span>
      </label>
      <select id="fc-gas-use" onchange="fcGasUseChange()">
        <option value="hw"   selected>Heating &amp; hot water</option>
        <option value="hwc"          >Heating, hot water &amp; cooking</option>
        <option value="hw_only"      >Hot water only (no heating)</option>
        <option value="hwc_only"     >Hot water &amp; cooking (no heating)</option>
      </select>
    </div>
    <div class="fc-field">
      <label for="fc-min-gas">
        Min. daily gas (kWh)
        <span class="fc-hint">floor for hot water / cooking when not heating</span>
      </label>
      <input type="number" id="fc-min-gas" step="0.5" min="0" value="2">
    </div>
    <div class="fc-field fc-btn-cell">
      <button class="fc-calc-btn" onclick="fcPredict()">Calculate</button>
    </div>
  </div>

  <div id="fc-results" style="display:none;margin-top:16px">
    <div class="fc-result-row">
      <span class="fc-fuel-label">&#x26A1; Electricity</span>
      <span id="fc-elec-val"   class="fc-kwh"></span>
      <span id="fc-elec-range" class="fc-range"></span>
    </div>
    <div class="fc-result-row">
      <span class="fc-fuel-label">&#x1F525; Gas</span>
      <span id="fc-gas-val"    class="fc-kwh"></span>
      <span id="fc-gas-range"  class="fc-range"></span>
    </div>
    <p id="fc-note" style="margin:8px 0 0;font-size:11px;color:#888"></p>
  </div>

  <details style="margin-top:18px">
    <summary style="cursor:pointer;font-size:12px;color:#666;user-select:none">
      Show fitted model equations ▸
    </summary>
    <div style="margin-top:10px;font-size:12px;line-height:1.8;
                background:#f8f9fa;padding:14px 16px;border-radius:6px;
                border-left:3px solid #ddd">
      {eq_elec}
      <br><br>
      {eq_gas}
      <br><br>
      <span style="color:#888;font-size:11px">
        The sin/cos month terms capture any residual seasonal pattern not
        already explained by temperature, sunshine, or solar elevation — for
        example, behavioural differences between months at the same temperature.<br>
        Max temperature is excluded: it is closely correlated with mean temperature,
        so including both would cause multicollinearity without adding explanatory
        power.
      </span>
    </div>
  </details>

  {normals_html}
</div>

<script>
(function() {{
  var FC_MODELS  = {_js_safe(models_json)};
  var FC_SOLAR   = {_js_safe(solar_json)};
  // 30-year WMO climate normals (1991-2020) — primary source for weather defaults
  var FC_NORMALS = {_js_safe(normals_weather_json)};

  // Baseline kWh/day by gas-use type (hot water / cooking only, no heating)
  var GAS_USE_MINS = {{ hw: 2, hwc: 4, hw_only: 2, hwc_only: 4 }};

  // Raw OLS prediction — may return negative on warm days; caller applies floor.
  function predict(model, tempMean, tempMin, sunshine, solar, month) {{
    if (!model) return null;
    var c   = model.coefficients;
    var hdd = Math.max(0, (model.hdd_base || 15.5) - tempMean);
    var sin = Math.sin(2 * Math.PI * month / 12);
    var cos = Math.cos(2 * Math.PI * month / 12);
    var p   = c[0] + c[1]*hdd      + c[2]*tempMin
                   + c[3]*sunshine  + c[4]*solar
                   + c[5]*sin       + c[6]*cos;
    var hw  = 2.0 * model.residual_std;   // ≈ 95 % prediction interval
    return {{ pred: p, low: p - hw, high: p + hw }};
  }}

  window.fcGasUseChange = function() {{
    var v = document.getElementById('fc-gas-use').value;
    document.getElementById('fc-min-gas').value = GAS_USE_MINS[v] || 2;
    document.getElementById('fc-results').style.display = 'none';
  }};

  window.fcMonthChange = function() {{
    var m      = document.getElementById('fc-month').value;
    var normals = FC_NORMALS[m];
    if (normals) {{
      document.getElementById('fc-temp-mean').value = normals.temp_mean;
      document.getElementById('fc-temp-min').value  = normals.temp_min;
      document.getElementById('fc-sunshine').value  = normals.sunshine_hours;
    }}
    document.getElementById('fc-solar').value = FC_SOLAR[m] || '';
    document.getElementById('fc-results').style.display = 'none';
  }};

  window.fcPredict = function() {{
    var month    = parseInt(document.getElementById('fc-month').value, 10);
    var tempMean = parseFloat(document.getElementById('fc-temp-mean').value);
    var tempMin  = parseFloat(document.getElementById('fc-temp-min').value);
    var sunshine = parseFloat(document.getElementById('fc-sunshine').value);
    var solar    = parseFloat(document.getElementById('fc-solar').value);
    var minGas   = parseFloat(document.getElementById('fc-min-gas').value) || 0;

    function fmt(r, floor) {{
      if (!r) return ['N/A \u2014 model not available', ''];
      var pred = Math.max(floor, r.pred);
      var low  = Math.max(floor, r.low);
      var high = Math.max(floor, r.high);
      return [pred.toFixed(1) + ' kWh / day',
              '95% range: ' + low.toFixed(1) + ' \u2013 ' + high.toFixed(1) + ' kWh'];
    }}

    var ef  = fmt(predict(FC_MODELS.electricity, tempMean, tempMin, sunshine, solar, month), 0);
    var gRaw = predict(FC_MODELS.gas,            tempMean, tempMin, sunshine, solar, month);
    var gf  = fmt(gRaw, minGas);

    document.getElementById('fc-elec-val').textContent   = ef[0];
    document.getElementById('fc-elec-range').textContent = ef[1];
    document.getElementById('fc-gas-val').textContent    = gf[0];
    document.getElementById('fc-gas-range').textContent  = gf[1];

    var note = 'Prediction interval: \u00b12\u03c3 of in-sample residuals. '
             + 'Actual consumption may differ due to household behaviour or unusual weather.';
    if (gRaw && gRaw.pred < minGas) {{
      note += ' Gas floored at ' + minGas.toFixed(1)
            + ' kWh/day (non-heating baseline for hot water'
            + (minGas >= 4 ? '/cooking' : '') + ').';
    }}
    document.getElementById('fc-note').textContent = note;
    document.getElementById('fc-results').style.display = 'block';
  }};

  // Initialise to current calendar month
  document.getElementById('fc-month').value = String(new Date().getMonth() + 1);
  fcMonthChange();
}})();
</script>
"""


# ── Build figures ─────────────────────────────────────────────────────────────
print("Building charts…")

fig_elec_usage = build_timeseries_fig(elec_aggs, "Electricity", "usage", "#1f77b4", "#aec7e8")
fig_elec_cost  = build_timeseries_fig(elec_aggs, "Electricity", "cost",  "#1f77b4", "#aec7e8")
fig_elec_hm    = build_heatmap_fig(elec_df, "Electricity", colorscale = "Blues")
fig_gas_usage  = build_timeseries_fig(gas_aggs, "Gas", "usage", "#d62728", "#f7b6b2")
fig_gas_cost   = build_timeseries_fig(gas_aggs, "Gas", "cost",  "#d62728", "#f7b6b2")
fig_gas_hm     = build_heatmap_fig(gas_df, "Gas", colorscale = "Oranges")
fig_profile    = build_profile_fig(elec_df, gas_df)

print("Pre-computing pattern chart data for filter bar…")
elec_hm_lookup  = _compute_heatmap_lookup(elec_df, DATA_YEARS)
gas_hm_lookup   = _compute_heatmap_lookup(gas_df,  DATA_YEARS)
profile_lookup  = _compute_profile_lookup(elec_df, gas_df, DATA_YEARS)

fig_rates_summary = build_rates_summary_fig(
    elec_summary, gas_summary, elec_tariff_code, gas_tariff_code
)

if HAS_WEATHER:
    print("Building weather charts…")
    fig_temp_scatter     = build_temp_scatter_fig(
        elec_aggs["day"], gas_aggs["day"], weather_df, WEATHER_LOCATION
    )
    fig_sunshine_scatter = build_sunshine_scatter_fig(
        elec_aggs["day"], gas_aggs["day"], weather_df
    )
    forecast_html = build_forecast_tool_html(energy_models, WEATHER_LOCATION,
                                              climate_normals)

# sections format: (heading, [(fig, div_id_or_None), ...])
# div_id is used for pattern charts so JS can target them by ID.
elec_figs = [(fig_elec_usage, None)]
if fig_elec_cost is not None:
    elec_figs.append((fig_elec_cost, None))
elec_figs.append((fig_elec_hm, "hm-elec"))

gas_figs = [(fig_gas_usage, None)]
if fig_gas_cost is not None:
    gas_figs.append((fig_gas_cost, None))
gas_figs.append((fig_gas_hm, "hm-gas"))

tariff_figs = [(fig_rates_summary, None)]
if has_elec_tariff:
    tariff_figs.append((build_tariff_fig(
        elec_rates_df, elec_sc_df,
        fuel_label = "Electricity", color = "#1f77b4",
        fill_color = "rgba(31, 119, 180, 0.12)",
    ), None))
if has_gas_tariff:
    tariff_figs.append((build_tariff_fig(
        gas_rates_df, gas_sc_df,
        fuel_label = "Gas", color = "#d62728",
        fill_color = "rgba(214, 39, 40, 0.10)",
    ), None))

sections = [
    ("&#x26A1; Electricity",     elec_figs),
    ("&#x1F525; Gas",            gas_figs),
    ("Combined Patterns",        [(fig_profile, "chart-profile")]),
]
if HAS_WEATHER:
    sections += [
        ("&#x1F321;&#xFE0F; Temperature &amp; Consumption", [
            (fig_temp_scatter,     None),
            (fig_sunshine_scatter, None),
        ]),
        ("&#x1F4CA; Consumption Forecast", [
            (forecast_html, None),
        ]),
    ]
sections.append(("&#x1F4CB; Tariff Details", tariff_figs))

# ── HTML export ───────────────────────────────────────────────────────────────

def build_dashboard_html(sections, output_path, data_years,
                         elec_hm_lookup, gas_hm_lookup, profile_lookup,
                         built_at=None):
    css = """
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           background: #f8f9fa; margin: 0; padding: 0; color: #2c3e50; }
    .fb-run-stamp { margin-left: auto; font-size: 10px; color: #7a9ab8; white-space: nowrap; }
    #filter-bar { position: sticky; top: 0; z-index: 100; background: #2c3e50; color: white;
                  padding: 10px 24px; display: flex; align-items: center; gap: 10px;
                  flex-wrap: wrap; box-shadow: 0 2px 8px rgba(0,0,0,0.25); }
    #filter-bar strong { font-size: 13px; white-space: nowrap; }
    .fb-group { display: flex; align-items: center; gap: 6px; }
    .fb-btn { background: #3d5166; border: 1px solid #5a7a99; color: white;
              padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
    .fb-btn:hover { background: #4a6380; }
    .fb-sep { width: 1px; height: 20px; background: #4a6a88; }
    .fb-date { background: #3d5166; border: 1px solid #5a7a99; color: white;
               padding: 3px 6px; border-radius: 4px; font-size: 12px; }
    /* Custom dropdown multi-select */
    .fb-dd { position: relative; display: inline-block; }
    .fb-dd-btn { background: #3d5166; border: 1px solid #5a7a99; color: #ccc;
                 padding: 4px 26px 4px 10px; border-radius: 4px; cursor: pointer;
                 font-size: 12px; white-space: nowrap; min-width: 90px; text-align: left;
                 position: relative; }
    .fb-dd-btn::after { content: "\\25BE"; position: absolute; right: 8px; top: 50%;
                        transform: translateY(-50%); font-size: 10px; color: #aac4dc; }
    .fb-dd-btn:hover { background: #4a6380; }
    .fb-dd-panel { display: none; position: absolute; top: calc(100% + 3px); left: 0;
                   min-width: 130px; background: #2c3e50; border: 1px solid #5a7a99;
                   border-radius: 4px; z-index: 200; padding: 4px 0;
                   max-height: 210px; overflow-y: auto;
                   box-shadow: 0 4px 14px rgba(0,0,0,0.35); }
    .fb-dd.open .fb-dd-panel { display: block; }
    .fb-dd-panel label { display: flex; align-items: center; gap: 7px;
                         padding: 5px 12px; color: #ccc; cursor: pointer; font-size: 12px; }
    .fb-dd-panel label:hover { background: #3a5068; }
    .fb-hint-text { font-size: 10px; color: #7a9ab8; display: block; margin-top: 2px; }
    .fb-label { font-size: 12px; color: #aac4dc; white-space: nowrap; }
    #main { padding: 24px; }
    h1 { border-bottom: 3px solid #3498db; padding-bottom: 12px; margin-bottom: 24px; }
    h2 { margin-top: 40px; margin-bottom: 16px; }
    .card { background: white; border-radius: 8px; padding: 12px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.08); margin-bottom: 20px; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
    @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
    /* Forecast tool */
    .forecast-tool { padding: 4px 0; }
    .fc-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
               gap: 12px 20px; align-items: end; margin-top: 4px; }
    .fc-field { display: flex; flex-direction: column; gap: 4px; }
    .fc-field label { font-size: 12px; color: #555; font-weight: 500; display: flex; flex-direction: column; }
    .fc-hint { font-size: 10px; color: #999; font-weight: 400; margin-top: 1px; }
    .fc-field input, .fc-field select {
        padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px;
        font-size: 13px; width: 100%; }
    .fc-btn-cell { justify-content: flex-end; }
    .fc-calc-btn { background: #2c3e50; color: white; border: none;
                   padding: 8px 20px; border-radius: 4px; cursor: pointer;
                   font-size: 13px; font-weight: 600; width: 100%; }
    .fc-calc-btn:hover { background: #3d5166; }
    .fc-result-row { display: flex; align-items: baseline; gap: 14px;
                     padding: 8px 12px; border-radius: 6px; margin-bottom: 6px;
                     background: #f4f6f8; }
    .fc-fuel-label { font-weight: 600; font-size: 14px; min-width: 110px; }
    .fc-kwh  { font-size: 18px; font-weight: 700; color: #2c3e50; }
    .fc-range { font-size: 12px; color: #666; }
    /* Climate normals reference table */
    .fc-ref-table { border-collapse: collapse; font-size: 12px; width: 100%; max-width: 480px; }
    .fc-ref-table th { background: #2c3e50; color: white; padding: 6px 10px;
                       text-align: center; font-weight: 500; line-height: 1.3; }
    .fc-ref-table td { padding: 5px 10px; text-align: center; border-bottom: 1px solid #eee; }
    .fc-ref-table tr:first-child td { font-weight: 600; }
    .fc-ref-table tr:nth-child(even) td { background: #f8f9fa; }
    /* Attribution footer */
    #attrib { text-align: center; padding: 16px 24px 20px;
              font-size: 11px; color: #aaa; border-top: 1px solid #e0e0e0;
              margin-top: 8px; }
    #attrib a { color: #888; }
    """

    year_checkboxes = "".join(
        f'<label><input type="checkbox" value="{y}"> {y}</label>'
        for y in data_years
    )
    month_checkboxes = "".join(
        f'<label><input type="checkbox" value="{i}"> {name}</label>'
        for i, name in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
        )
    )

    filter_bar = f"""<div id="filter-bar">
  <strong>Filter:</strong>
  <div class="fb-group">
    <span class="fb-label">Year</span>
    <div class="fb-dd" id="dd-year">
      <button class="fb-dd-btn" onclick="ddToggle('dd-year')">All years</button>
      <div class="fb-dd-panel" data-all-label="All years">{year_checkboxes}</div>
    </div>
    <span class="fb-hint-text">click to select multiple</span>
  </div>
  <div class="fb-group">
    <span class="fb-label">Month</span>
    <div class="fb-dd" id="dd-month">
      <button class="fb-dd-btn" onclick="ddToggle('dd-month')">All months</button>
      <div class="fb-dd-panel" data-all-label="All months">{month_checkboxes}</div>
    </div>
    <span class="fb-hint-text">click to select multiple</span>
  </div>
  <div class="fb-sep"></div>
  <div class="fb-group">
    <span class="fb-label">Custom range</span>
    <input type="date" id="fb-from" class="fb-date" title="From">
    <span class="fb-label">to</span>
    <input type="date" id="fb-to"   class="fb-date" title="To">
    <button class="fb-btn" onclick="applyCustomRange()">Apply</button>
  </div>
  <button class="fb-btn" onclick="clearFilters()">Reset</button>
  <span class="fb-run-stamp">Updated {built_at}</span>
</div>"""

    import json as _json
    hm_elec_json  = _json.dumps(elec_hm_lookup)
    hm_gas_json   = _json.dumps(gas_hm_lookup)
    profile_json  = _json.dumps(profile_lookup)

    # JS injected at end of body — runs after Plotly has initialised all charts.
    # Two filter pathways:
    #   1. Timeseries charts (date x-axis): x-axis range + per-bar opacity via Plotly.update()
    #   2. Pattern charts (heatmap, profile): data swapped from pre-computed lookups
    js = f"""<script>
// ── Pre-computed pattern-chart lookups ────────────────────────────────────────
// Keys: year ("all"|"2024"|...), then month ("0"=all | "1"–"12")
// Each heatmap leaf: {{z: <matrix>, n: <count>}}
// Each profile leaf: {{elec: {{y: <array>, n: <count>}}, gas: {{y: <array>, n: <count>}}}}
var HM_ELEC   = {hm_elec_json};
var HM_GAS    = {hm_gas_json};
var PROFILE   = {profile_json};

// ── Dropdown helpers ──────────────────────────────────────────────────────────
function ddToggle(id) {{
    var dd = document.getElementById(id);
    var wasOpen = dd.classList.contains('open');
    document.querySelectorAll('.fb-dd.open').forEach(function(d) {{ d.classList.remove('open'); }});
    if (!wasOpen) dd.classList.add('open');
}}

function ddUpdateLabel(dd) {{
    var checks = Array.from(dd.querySelectorAll('input[type=checkbox]:checked'));
    var btn    = dd.querySelector('.fb-dd-btn');
    var all    = dd.querySelector('.fb-dd-panel').dataset.allLabel || 'All';
    if (checks.length === 0) {{
        btn.textContent = all;
    }} else if (checks.length === 1) {{
        btn.textContent = checks[0].closest('label').textContent.trim();
    }} else {{
        btn.textContent = checks.length + ' selected';
    }}
}}

// Close dropdowns when clicking outside
document.addEventListener('click', function(e) {{
    if (!e.target.closest('.fb-dd')) {{
        document.querySelectorAll('.fb-dd.open').forEach(function(d) {{ d.classList.remove('open'); }});
    }}
}});

// Wire checkbox changes: update label then run filters
document.querySelectorAll('.fb-dd-panel').forEach(function(panel) {{
    panel.addEventListener('change', function() {{
        ddUpdateLabel(panel.closest('.fb-dd'));
        applyFilters();
    }});
}});

// Return array of checked checkbox values for a dropdown, or null (= all).
function getSelectedValues(ddId) {{
    var checks = Array.from(document.querySelectorAll('#' + ddId + ' input[type=checkbox]:checked'));
    var vals = checks.map(function(c) {{ return c.value; }});
    return vals.length ? vals : null;
}}

function ddClearAll(ddId) {{
    var dd = document.getElementById(ddId);
    dd.querySelectorAll('input[type=checkbox]').forEach(function(c) {{ c.checked = false; }});
    ddUpdateLabel(dd);
}}

// ── Timeseries helpers ────────────────────────────────────────────────────────
function getTimeDivs() {{
    return Array.from(document.querySelectorAll('.js-plotly-plot')).filter(function(d) {{
        return d._fullLayout && d._fullLayout.xaxis && d._fullLayout.xaxis.type === 'date';
    }});
}}

// True when a trace's x values are ISO date strings (e.g. "2024-01-15").
function hasDateX(trace) {{
    return trace.x && trace.x.length > 0 && /^\\d{{4}}-\\d{{2}}/.test(String(trace.x[0]));
}}

// Apply x-axis range + optional month-fade to every timeseries chart.
// yearVals : array of year strings, or null (all years).
// monthVals: array of month ints 1-12, or null (all months).
function _applyToTimeseries(from, to, yearVals, monthVals) {{
    var layoutUpdate = (from || to)
        ? {{'xaxis.range[0]': from, 'xaxis.range[1]': to, 'xaxis.autorange': false}}
        : {{'xaxis.autorange': true}};
    getTimeDivs().forEach(function(div) {{
        var traceIndices = [], opacities = [];
        div.data.forEach(function(trace, i) {{
            if (!hasDateX(trace)) return;
            traceIndices.push(i);
            opacities.push(
                (monthVals && monthVals.length)
                ? trace.x.map(function(x) {{
                      return monthVals.indexOf(new Date(x).getMonth() + 1) >= 0 ? 1.0 : 0.1;
                  }})
                : 1.0
            );
        }});
        if (traceIndices.length > 0) {{
            Plotly.update(div, {{'marker.opacity': opacities}}, layoutUpdate, traceIndices);
        }} else {{
            Plotly.relayout(div, layoutUpdate);
        }}
    }});
}}

// ── Pattern-chart helpers ─────────────────────────────────────────────────────

// Weighted-average z matrix across all selected year/month combinations.
// yearVals/monthVals are arrays of strings, or null (= all).
function _combineHeatmaps(lookup, yearVals, monthVals) {{
    var allYears  = yearVals  ? yearVals  : Object.keys(lookup);
    var allMonths = monthVals ? monthVals : ['0'];
    var sumZ = null, totalN = 0;
    allYears.forEach(function(yr) {{
        var yb = lookup[yr] || {{}};
        allMonths.forEach(function(mo) {{
            var entry = yb[mo];
            if (!entry || !entry.z || !entry.n) return;
            var n = entry.n, zmat = entry.z;
            if (sumZ === null) {{
                sumZ = zmat.map(function(row) {{
                    return row.map(function(v) {{ return (v === null ? 0 : v) * n; }});
                }});
            }} else {{
                zmat.forEach(function(row, r) {{
                    row.forEach(function(v, c) {{ if (v !== null) sumZ[r][c] += v * n; }});
                }});
            }}
            totalN += n;
        }});
    }});
    if (!sumZ || totalN === 0) return null;
    return sumZ.map(function(row) {{
        return row.map(function(v) {{ return v / totalN; }});
    }});
}}

function _updateHeatmap(divId, lookup, yearVals, monthVals) {{
    var div = document.getElementById(divId);
    if (!div) return;
    var z = _combineHeatmaps(lookup, yearVals, monthVals);
    if (!z) return;
    Plotly.restyle(div, {{z: [z], zauto: [true]}}, [0]);
}}

// Weighted-average y array for one fuel across selected year/month combinations.
function _combineProfiles(fuelKey, lookup, yearVals, monthVals) {{
    var allYears  = yearVals  ? yearVals  : Object.keys(lookup);
    var allMonths = monthVals ? monthVals : ['0'];
    var sumY = null, totalN = 0;
    allYears.forEach(function(yr) {{
        var yb = lookup[yr] || {{}};
        allMonths.forEach(function(mo) {{
            var entry = (yb[mo] || {{}})[fuelKey];
            if (!entry || !entry.y || !entry.n) return;
            var n = entry.n, arr = entry.y;
            if (sumY === null) {{
                sumY = arr.map(function(v) {{ return (v === null ? 0 : v) * n; }});
            }} else {{
                arr.forEach(function(v, i) {{ if (v !== null) sumY[i] += v * n; }});
            }}
            totalN += n;
        }});
    }});
    if (!sumY || totalN === 0) return null;
    return sumY.map(function(v) {{ return v / totalN; }});
}}

function _updateProfile(lookup, yearVals, monthVals) {{
    var div = document.getElementById('chart-profile');
    if (!div) return;
    var ys = [], traces = [];
    var elecY = _combineProfiles('elec', lookup, yearVals, monthVals);
    var gasY  = _combineProfiles('gas',  lookup, yearVals, monthVals);
    if (elecY) {{ ys.push(elecY); traces.push(0); }}
    if (gasY)  {{ ys.push(gasY);  traces.push(1); }}
    if (ys.length) Plotly.restyle(div, {{y: ys}}, traces);
}}

// ── Public API ────────────────────────────────────────────────────────────────
function applyFilters() {{
    var rawYears  = getSelectedValues('dd-year');
    var rawMonths = getSelectedValues('dd-month');
    var monthInts = rawMonths
        ? rawMonths.map(function(m) {{ return parseInt(m, 10); }})
        : null;
    // Restrict timeseries x-axis only when exactly one year is chosen
    var from = null, to = null;
    if (rawYears && rawYears.length === 1) {{
        from = rawYears[0] + '-01-01';
        to   = (parseInt(rawYears[0], 10) + 1) + '-01-01';
    }}
    _applyToTimeseries(from, to, rawYears, monthInts);
    _updateHeatmap('hm-elec', HM_ELEC, rawYears, rawMonths);
    _updateHeatmap('hm-gas',  HM_GAS,  rawYears, rawMonths);
    _updateProfile(PROFILE, rawYears, rawMonths);
}}

function applyCustomRange() {{
    var from = document.getElementById('fb-from').value || null;
    var to   = document.getElementById('fb-to').value   || null;
    ddClearAll('dd-year');
    ddClearAll('dd-month');
    _applyToTimeseries(from, to, null, null);
}}

function clearFilters() {{
    ddClearAll('dd-year');
    ddClearAll('dd-month');
    document.getElementById('fb-from').value = '';
    document.getElementById('fb-to').value   = '';
    _applyToTimeseries(null, null, null, null);
    _updateHeatmap('hm-elec', HM_ELEC, null, null);
    _updateHeatmap('hm-gas',  HM_GAS,  null, null);
    _updateProfile(PROFILE, null, null);
}}
</script>"""

    parts = [
        '<!DOCTYPE html><html lang="en"><head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>Energy Usage Dashboard</title>',
        f'<style>{css}</style>',
        '</head><body>',
        filter_bar,
        '<div id="main">',
        '<h1>&#x26A1; Energy Usage Dashboard</h1>\n',
    ]

    include_js = True
    for heading, figs in sections:
        parts.append(f"<h2>{heading}</h2>\n")
        for fig, div_id in figs:
            if isinstance(fig, str):
                # Raw HTML content (e.g. the forecast tool)
                parts.append(f'<div class="card">{fig}</div>\n')
            else:
                kwargs = {"div_id": div_id} if div_id else {}
                div = pio.to_html(fig, full_html = False,
                                  include_plotlyjs = "cdn" if include_js else False,
                                  **kwargs)
                include_js = False
                parts.append(f'<div class="card">{div}</div>\n')

    parts.append('</div>')   # close #main
    parts.append(
        '<footer id="attrib">'
        'Weather data by <a href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo.com</a>'
        ' — licensed under <a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noopener">CC BY 4.0</a>'
        '</footer>'
    )
    parts.append(js)
    parts.append('</body></html>')

    os.makedirs(os.path.dirname(output_path), exist_ok = True)
    with open(output_path, "w", encoding = "utf-8") as f:
        f.write("".join(parts))
    print(f"Dashboard written: {output_path}")


output_html = os.path.join(OUTPUT_DIR, "dashboard.html")
_built_at   = pd.Timestamp.now().strftime("%d %b %Y %H:%M")
build_dashboard_html(sections, output_html, DATA_YEARS,
                     elec_hm_lookup, gas_hm_lookup, profile_lookup,
                     built_at=_built_at)
