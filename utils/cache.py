"""
Simple local caching layer using Parquet files.

Why cache?
----------
Fetching a full history of half-hourly readings (potentially years of data) takes
several seconds and counts against API rate limits.  Caching to Parquet lets the
notebook reload in < 1 s on repeat runs while keeping the raw data easy to inspect
with any Parquet-aware tool.

Usage
-----
    from utils import cache

    CACHE_DIR = os.path.join(project_root, "data", "cache")

    df = cache.load("electricity_raw", CACHE_DIR)
    if df is None:
        df = fetch_and_build(...)
        cache.save(df, "electricity_raw", CACHE_DIR)
"""

import os
from typing import Optional

import pandas as pd


def _path(name: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"{name}.parquet")


def exists(name: str, cache_dir: str) -> bool:
    """Return True if a cached file exists for this name."""
    return os.path.exists(_path(name, cache_dir))


def save(df: pd.DataFrame, name: str, cache_dir: str) -> str:
    """
    Save a DataFrame to *cache_dir*/<name>.parquet.

    Creates *cache_dir* if it does not exist.  Returns the path written.
    """
    os.makedirs(cache_dir, exist_ok = True)
    path = _path(name, cache_dir)
    df.to_parquet(path, index = False)
    return path


def load(name: str, cache_dir: str) -> Optional[pd.DataFrame]:
    """
    Load a cached DataFrame.  Returns None if the file does not exist.
    """
    path = _path(name, cache_dir)
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)


def invalidate(name: str, cache_dir: str) -> bool:
    """
    Delete a cached file.  Returns True if the file existed and was removed.
    """
    path = _path(name, cache_dir)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def refresh(
    df: pd.DataFrame,
    name: str,
    cache_dir: str,
    new_records: list,
    build_fn,
) -> pd.DataFrame:
    """
    Append new API records to an existing cached DataFrame and re-save.

    Parameters
    ----------
    df          : The existing cached DataFrame (already loaded)
    name        : Cache name (used for the Parquet filename)
    cache_dir   : Directory containing cache files
    new_records : Raw API records fetched since the last cached timestamp
    build_fn    : Callable(records) → DataFrame — converts raw records to a
                  DataFrame using the same logic as the initial build
                  (e.g. ``lambda r: transform.consumption_to_df(r, 'electricity')``)

    Returns the combined DataFrame (existing + new rows), deduped and sorted.
    """
    new_df = build_fn(new_records)
    combined = pd.concat([df, new_df], ignore_index = True)
    combined = combined.drop_duplicates(subset = ["interval_start"]).sort_values(
        "interval_start"
    ).reset_index(drop = True)
    save(combined, name, cache_dir)
    return combined
