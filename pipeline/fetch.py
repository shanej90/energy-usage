"""
Octopus Energy REST API data fetching.

All consumption and tariff endpoints are covered here.  Pagination is handled
transparently — callers always receive a flat list of all records regardless of
how many API pages are required.

Authentication
--------------
- Consumption endpoints: HTTP Basic Auth (API key as username, empty password).
- Tariff / product endpoints: no auth required (public data).
- GraphQL endpoints: use utils.auth.get_token() separately.

Tariff code format
------------------
  E-1R-AGILE-24-04-03-A
  │ │  └─────────────┘ └── region code (single letter)
  │ └────────────────────── rate type (1R = single register)
  └──────────────────────── fuel (E = electricity, G = gas)

The product code embedded in the tariff code is everything between the rate type
and the region suffix: 'AGILE-24-04-03' in the example above.
"""

from typing import Optional

import requests

BASE_URL = "https://api.octopus.energy/v1"
_DEFAULT_PAGE_SIZE = 25_000  # max allowed by the API


def _fetch_paginated(
    session: requests.Session,
    url: str,
    params: Optional[dict] = None,
    auth=None,
) -> list:
    """
    Iterate through all pages of a paginated Octopus REST endpoint.

    The first request uses *params*; subsequent requests follow the 'next' URL
    returned in each response (which already has all query parameters encoded).
    """
    results = []
    while url:
        r = session.get(url, params = params, auth = auth, timeout = 30)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        url = data.get("next")
        params = None  # encoded in the 'next' URL from here on
    return results


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------

def fetch_consumption(
    session: requests.Session,
    fuel: str,
    mpxn: str,
    serial: str,
    api_key: str,
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> list:
    """
    Fetch half-hourly consumption records.

    Parameters
    ----------
    fuel        : 'electricity' or 'gas'
    mpxn        : MPAN (electricity) or MPRN (gas)
    serial      : Meter serial number
    api_key     : Octopus API key — used as the HTTP Basic Auth username
    period_from : ISO 8601 start timestamp, e.g. '2024-01-01T00:00:00Z'
    period_to   : ISO 8601 end timestamp
    page_size   : Records per page (max 25 000)

    Returns
    -------
    List of dicts: [{consumption, interval_start, interval_end}, ...]
      - electricity: consumption is in kWh
      - gas SMETS1:  consumption is in kWh
      - gas SMETS2:  consumption is in m³ (see transform.consumption_to_df)
    """
    if fuel == "electricity":
        url = f"{BASE_URL}/electricity-meter-points/{mpxn}/meters/{serial}/consumption/"
    elif fuel == "gas":
        url = f"{BASE_URL}/gas-meter-points/{mpxn}/meters/{serial}/consumption/"
    else:
        raise ValueError(f"fuel must be 'electricity' or 'gas', got {fuel!r}")

    params = {"page_size": page_size, "order_by": "period"}
    if period_from:
        params["period_from"] = period_from
    if period_to:
        params["period_to"] = period_to

    auth = requests.auth.HTTPBasicAuth(api_key, "")
    return _fetch_paginated(session, url, params = params, auth = auth)


# ---------------------------------------------------------------------------
# Tariff rates (public — no auth required)
# ---------------------------------------------------------------------------

def fetch_unit_rates(
    session: requests.Session,
    fuel: str,
    product_code: str,
    tariff_code: str,
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
) -> list:
    """
    Fetch unit rate history for a tariff.

    For time-of-use tariffs (e.g. Agile) this returns one record per 30-minute
    slot.  For fixed tariffs there is typically one record per price change.

    Returns
    -------
    List of dicts: [{value_exc_vat, value_inc_vat, valid_from, valid_to}, ...]
    Prices are in pence per kWh.
    """
    if fuel == "electricity":
        url = (
            f"{BASE_URL}/products/{product_code}"
            f"/electricity-tariffs/{tariff_code}/standard-unit-rates/"
        )
    else:
        url = (
            f"{BASE_URL}/products/{product_code}"
            f"/gas-tariffs/{tariff_code}/standard-unit-rates/"
        )

    params: dict = {"page_size": _DEFAULT_PAGE_SIZE}
    if period_from:
        params["period_from"] = period_from
    if period_to:
        params["period_to"] = period_to

    return _fetch_paginated(session, url, params = params)


def fetch_standing_charges(
    session: requests.Session,
    fuel: str,
    product_code: str,
    tariff_code: str,
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
) -> list:
    """
    Fetch standing charge history for a tariff.

    Returns
    -------
    List of dicts: [{value_exc_vat, value_inc_vat, valid_from, valid_to}, ...]
    Prices are in pence per day.
    """
    if fuel == "electricity":
        url = (
            f"{BASE_URL}/products/{product_code}"
            f"/electricity-tariffs/{tariff_code}/standing-charges/"
        )
    else:
        url = (
            f"{BASE_URL}/products/{product_code}"
            f"/gas-tariffs/{tariff_code}/standing-charges/"
        )

    params: dict = {"page_size": _DEFAULT_PAGE_SIZE}
    if period_from:
        params["period_from"] = period_from
    if period_to:
        params["period_to"] = period_to

    return _fetch_paginated(session, url, params = params)


# ---------------------------------------------------------------------------
# Meter-point agreements (tariff history)
# ---------------------------------------------------------------------------

def fetch_agreements(
    session: requests.Session,
    fuel: str,
    mpxn: str,
    api_key: str,
) -> list:
    """
    Return all tariff agreements for a meter point, oldest first.

    Calls /v1/electricity-meter-points/{mpan}/ or /v1/gas-meter-points/{mprn}/
    with Basic Auth and extracts the ``agreements`` array.

    Parameters
    ----------
    fuel   : 'electricity' or 'gas'
    mpxn   : MPAN (electricity) or MPRN (gas)
    api_key: Octopus API key — used as the HTTP Basic Auth username

    Returns
    -------
    List of dicts sorted by valid_from:
      [{tariff_code, valid_from, valid_to}, ...]
    valid_to is None (or absent) for the currently active agreement.
    """
    if fuel == "electricity":
        url = f"{BASE_URL}/electricity-meter-points/{mpxn}/"
    else:
        url = f"{BASE_URL}/gas-meter-points/{mpxn}/"

    auth = requests.auth.HTTPBasicAuth(api_key, "")
    r = session.get(url, auth = auth, timeout = 30)
    if r.status_code == 404:
        return []          # meter point not found
    r.raise_for_status()
    agreements = r.json().get("agreements", [])
    return sorted(agreements, key = lambda a: a.get("valid_from") or "")


# ---------------------------------------------------------------------------
# Account-level data (tariff agreement history)
# ---------------------------------------------------------------------------

def fetch_account_number(
    session: requests.Session,
    api_key: str,
) -> Optional[str]:
    """
    Return the Octopus account number for the given API key.

    Uses the GraphQL API (obtainKrakenToken → viewer query) — there is no REST
    endpoint that returns the account number without already knowing it.

    Returns None if no accounts are found.
    """
    endpoint = f"{BASE_URL}/graphql/"
    # Step 1: exchange API key for a short-lived JWT
    token_resp = session.post(
        endpoint,
        json = {
            "query": """
            mutation ObtainToken($k: String!) {
                obtainKrakenToken(input: {APIKey: $k}) { token }
            }
            """,
            "variables": {"k": api_key},
        },
        headers = {"Content-Type": "application/json"},
        timeout = 30,
    )
    token_resp.raise_for_status()
    token = token_resp.json()["data"]["obtainKrakenToken"]["token"]

    # Step 2: query the account number
    viewer_resp = session.post(
        endpoint,
        json = {"query": "{ viewer { accounts { number } } }"},
        headers = {"Authorization": token, "Content-Type": "application/json"},
        timeout = 30,
    )
    viewer_resp.raise_for_status()
    accounts = viewer_resp.json()["data"]["viewer"]["accounts"]
    return accounts[0]["number"] if accounts else None


def fetch_account_agreements(
    session: requests.Session,
    account_number: str,
    api_key: str,
) -> tuple:
    """
    Fetch tariff agreement history for all meters in an account.

    Calls ``GET /v1/accounts/{account_number}/`` (Basic Auth) and walks the
    nested properties → meter_points → agreements structure.

    Returns
    -------
    (elec_by_mpan, gas_by_mprn) where each is a dict:
      {mpan_or_mprn: [sorted list of {tariff_code, valid_from, valid_to}, ...]}

    valid_to is None for the currently active agreement.
    """
    url  = f"{BASE_URL}/accounts/{account_number}/"
    auth = requests.auth.HTTPBasicAuth(api_key, "")
    r    = session.get(url, auth = auth, timeout = 30)
    r.raise_for_status()
    data = r.json()

    elec_by_mpan: dict = {}
    gas_by_mprn:  dict = {}

    for prop in data.get("properties", []):
        for emp in prop.get("electricity_meter_points", []):
            mpan = str(emp["mpan"])
            elec_by_mpan[mpan] = sorted(
                emp.get("agreements", []),
                key = lambda a: a.get("valid_from") or "",
            )
        for gmp in prop.get("gas_meter_points", []):
            mprn = str(gmp["mprn"])
            gas_by_mprn[mprn] = sorted(
                gmp.get("agreements", []),
                key = lambda a: a.get("valid_from") or "",
            )

    return elec_by_mpan, gas_by_mprn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_product_code(tariff_code: str) -> str:
    """
    Derive the product code from a tariff code.

    Examples
    --------
    'E-1R-AGILE-24-04-03-A'  →  'AGILE-24-04-03'
    'G-1R-SILVER-23-12-06-A' →  'SILVER-23-12-06'
    'E-2R-VAR-22-11-01-A'    →  'VAR-22-11-01'
    """
    parts = tariff_code.split("-")
    # parts[0] = fuel prefix (E/G)
    # parts[1] = rate type (1R, 2R, …)
    # parts[-1] = region code (single letter)
    # everything in between is the product code
    return "-".join(parts[2:-1])
