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
    GAS_MPRN, GAS_SERIAL, ELECTRICITY_TARIFF_CODE, GAS_TARIFF_CODE
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
        hm  = T.heatmap_data(subset)
        mat = hm.set_index("dow").reindex(range(7))[hours]
        return [
            [None if pd.isna(v) else round(float(v), 4) for v in row]
            for row in mat.values.tolist()
        ]

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
        prof = T.daily_profile(subset).set_index("hour").reindex(range(24))
        return [
            None if pd.isna(v) else round(float(v), 4)
            for v in prof["avg_consumption_kwh"].tolist()
        ]

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
    ("&#x1F4CB; Tariff Details", tariff_figs),
]

# ── HTML export ───────────────────────────────────────────────────────────────

def build_dashboard_html(sections, output_path, data_years,
                         elec_hm_lookup, gas_hm_lookup, profile_lookup):
    css = """
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           background: #f8f9fa; margin: 0; padding: 0; color: #2c3e50; }
    #filter-bar { position: sticky; top: 0; z-index: 100; background: #2c3e50; color: white;
                  padding: 10px 24px; display: flex; align-items: center; gap: 10px;
                  flex-wrap: wrap; box-shadow: 0 2px 8px rgba(0,0,0,0.25); }
    #filter-bar strong { font-size: 13px; white-space: nowrap; }
    .fb-group { display: flex; align-items: center; gap: 6px; }
    .fb-btn { background: #3d5166; border: 1px solid #5a7a99; color: white;
              padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
    .fb-btn:hover { background: #4a6380; }
    .fb-sep { width: 1px; height: 20px; background: #4a6a88; }
    .fb-select, .fb-date {
        background: #3d5166; border: 1px solid #5a7a99; color: white;
        padding: 3px 6px; border-radius: 4px; font-size: 12px; }
    .fb-select option { background: #2c3e50; }
    .fb-label { font-size: 12px; color: #aac4dc; white-space: nowrap; }
    #main { padding: 24px; }
    h1 { border-bottom: 3px solid #3498db; padding-bottom: 12px; margin-bottom: 24px; }
    h2 { margin-top: 40px; margin-bottom: 16px; }
    .card { background: white; border-radius: 8px; padding: 12px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.08); margin-bottom: 20px; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
    @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
    """

    month_options = "".join(
        f'<option value="{i}">{name}</option>'
        for i, name in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start = 1
        )
    )
    year_options = "".join(
        f'<option value="{y}">{y}</option>' for y in data_years
    )

    filter_bar = f"""<div id="filter-bar">
  <strong>Filter:</strong>
  <div class="fb-group">
    <span class="fb-label">Year</span>
    <select id="sel-year" class="fb-select" onchange="applyFilters()">
      <option value="all">All years</option>
      {year_options}
    </select>
  </div>
  <div class="fb-group">
    <span class="fb-label">Month of year</span>
    <select id="sel-month" class="fb-select" onchange="applyFilters()">
      <option value="0">All months</option>
      {month_options}
    </select>
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
var HM_ELEC   = {hm_elec_json};
var HM_GAS    = {hm_gas_json};
var PROFILE   = {profile_json};

// ── Timeseries helpers ────────────────────────────────────────────────────────
function getTimeDivs() {{
    return Array.from(document.querySelectorAll('.js-plotly-plot')).filter(function(d) {{
        return d._fullLayout && d._fullLayout.xaxis && d._fullLayout.xaxis.type === 'date';
    }});
}}

// True when a trace's x values are ISO date strings (e.g. "2024-01-15").
// Skips heatmap hour labels ("00:00") and profile integer x values.
function hasDateX(trace) {{
    return trace.x && trace.x.length > 0 && /^\\d{{4}}-\\d{{2}}/.test(String(trace.x[0]));
}}

// Apply x-axis range + optional month-fade to every timeseries chart atomically.
// from/to : ISO date strings or null (autorange).  month : 1-12 or 0 (all).
function _applyToTimeseries(from, to, month) {{
    var layoutUpdate = (from || to)
        ? {{'xaxis.range[0]': from, 'xaxis.range[1]': to, 'xaxis.autorange': false}}
        : {{'xaxis.autorange': true}};

    getTimeDivs().forEach(function(div) {{
        var traceIndices = [], opacities = [];
        div.data.forEach(function(trace, i) {{
            if (!hasDateX(trace)) return;
            traceIndices.push(i);
            opacities.push(month
                ? trace.x.map(function(x) {{
                      return (new Date(x).getMonth() + 1 === month) ? 1.0 : 0.1;
                  }})
                : 1.0);   // scalar resets all bars to full opacity
        }});
        if (traceIndices.length > 0) {{
            Plotly.update(div, {{'marker.opacity': opacities}}, layoutUpdate, traceIndices);
        }} else {{
            Plotly.relayout(div, layoutUpdate);
        }}
    }});
}}

// ── Pattern-chart helpers ─────────────────────────────────────────────────────
// Heatmap: swap z-values for the single trace and reset colorscale bounds.
function _updateHeatmap(divId, lookup, yearVal, monthVal) {{
    var div = document.getElementById(divId);
    if (!div) return;
    var d = (lookup[yearVal] || {{}})[String(monthVal)];
    if (!d) return;   // no data for this combination — leave chart as-is
    // zauto:true forces Plotly to recalculate the colorscale bounds after swap.
    Plotly.restyle(div, {{z: [d], zauto: [true]}}, [0]);
}}

// Profile: swap y-values for both fuel traces.
// Plotly.restyle data-update syntax: {{y: [val_t0, val_t1]}} with traceIndices [0, 1].
// Note: 'y[0]' notation is for relayout (layout paths), NOT for restyle data keys.
function _updateProfile(lookup, yearVal, monthVal) {{
    var div = document.getElementById('chart-profile');
    if (!div) return;
    var d = (lookup[yearVal] || {{}})[String(monthVal)];
    if (!d) return;
    var ys = [], traces = [];
    if (d.elec) {{ ys.push(d.elec); traces.push(0); }}
    if (d.gas)  {{ ys.push(d.gas);  traces.push(1); }}
    if (ys.length) Plotly.restyle(div, {{y: ys}}, traces);
}}

// ── Public API ────────────────────────────────────────────────────────────────
function applyFilters() {{
    var yearVal  = document.getElementById('sel-year').value;
    var monthVal = parseInt(document.getElementById('sel-month').value, 10);
    var from = null, to = null;
    if (yearVal !== 'all') {{
        from = yearVal + '-01-01';
        to   = (parseInt(yearVal, 10) + 1) + '-01-01';
    }}
    _applyToTimeseries(from, to, monthVal);
    _updateHeatmap('hm-elec',      HM_ELEC,  yearVal, monthVal);
    _updateHeatmap('hm-gas',       HM_GAS,   yearVal, monthVal);
    _updateProfile(PROFILE, yearVal, monthVal);
}}

function applyCustomRange() {{
    var from = document.getElementById('fb-from').value || null;
    var to   = document.getElementById('fb-to').value   || null;
    document.getElementById('sel-year').value  = 'all';
    document.getElementById('sel-month').value = '0';
    _applyToTimeseries(from, to, 0);
    // Custom date range doesn't affect pattern charts (they have no date axis)
}}

function clearFilters() {{
    document.getElementById('sel-year').value  = 'all';
    document.getElementById('sel-month').value = '0';
    document.getElementById('fb-from').value   = '';
    document.getElementById('fb-to').value     = '';
    _applyToTimeseries(null, null, 0);
    _updateHeatmap('hm-elec', HM_ELEC, 'all', 0);
    _updateHeatmap('hm-gas',  HM_GAS,  'all', 0);
    _updateProfile(PROFILE, 'all', 0);
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
            kwargs = {"div_id": div_id} if div_id else {}
            div = pio.to_html(fig, full_html = False,
                              include_plotlyjs = "cdn" if include_js else False,
                              **kwargs)
            include_js = False
            parts.append(f'<div class="card">{div}</div>\n')

    parts.append('</div>')   # close #main
    parts.append(js)
    parts.append('</body></html>')

    os.makedirs(os.path.dirname(output_path), exist_ok = True)
    with open(output_path, "w", encoding = "utf-8") as f:
        f.write("".join(parts))
    print(f"Dashboard written: {output_path}")


output_html = os.path.join(OUTPUT_DIR, "dashboard.html")
build_dashboard_html(sections, output_html, DATA_YEARS,
                     elec_hm_lookup, gas_hm_lookup, profile_lookup)
