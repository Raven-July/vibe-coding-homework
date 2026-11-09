#!/usr/bin/env python3
"""Clean Divvy trips, harmonize stations, and join NOAA daily weather.

This script emits aggregate/derived data only. Raw Divvy rows remain under the
git-ignored ``data/raw`` tree and are never copied to tracked outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype
from sklearn.cluster import DBSCAN


ROOT = Path(__file__).resolve().parents[1]
DIVVY_DIR = ROOT / "data/raw/divvy"
NOAA_DIR = ROOT / "data/raw/noaa"
PROCESSED_DIR = ROOT / "data/processed"
TABLE_DIR = ROOT / "outputs/tables"
SOURCE = "Divvy System Data & NOAA NCEI"
OHARE = "USW00094846"
MIDWAY = "USW00014819"
WEATHER_METRICS = ("PRCP", "SNWD", "SNOW", "AWND", "TMAX", "TMIN")
CHICAGO_BOUNDS = {"lat_min": 41.60, "lat_max": 42.10, "lng_min": -87.95, "lng_max": -87.50}
EARTH_RADIUS_M = 6_371_008.8
LOG = logging.getLogger("clean_and_merge")

TRIP_ALIASES = {
    "ride_id": ("ride_id", "trip_id"),
    "rideable_type": ("rideable_type", "bikeid"),
    "started_at": ("started_at", "start_time", "starttime"),
    "ended_at": ("ended_at", "end_time", "stoptime"),
    "start_station_name": ("start_station_name", "from_station_name"),
    "start_station_id": ("start_station_id", "from_station_id"),
    "end_station_name": ("end_station_name", "to_station_name"),
    "end_station_id": ("end_station_id", "to_station_id"),
    "start_lat": ("start_lat", "from_latitude"),
    "start_lng": ("start_lng", "start_lon", "from_longitude"),
    "end_lat": ("end_lat", "to_latitude"),
    "end_lng": ("end_lng", "end_lon", "to_longitude"),
    "member_casual": ("member_casual", "usertype", "user_type"),
}
REQUIRED_TRIP_FIELDS = tuple(TRIP_ALIASES)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--noaa-input-units",
        choices=("auto", "metric", "standard"),
        default="auto",
        help="NOAA source units; standard means Fahrenheit/inches/mph (default: infer).",
    )
    parser.add_argument("--accessed-date", default=date.today().isoformat())
    parser.add_argument(
        "--allow-incomplete-weather",
        action="store_true",
        help="Permit trip dates with weather fields still missing after Midway imputation.",
    )
    args = parser.parse_args()
    try:
        date.fromisoformat(args.accessed_date)
    except ValueError:
        parser.error("--accessed-date must be YYYY-MM-DD")
    return args


def setup_dirs() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    TABLE_DIR.mkdir(parents=True, exist_ok=True)


def csv_sources(folder: Path) -> list[tuple[Path, str | None]]:
    sources: list[tuple[Path, str | None]] = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        lower = path.name.lower()
        if lower.endswith((".csv", ".csv.gz")):
            sources.append((path, None))
        elif lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(path) as archive:
                    sources.extend((path, name) for name in archive.namelist() if name.lower().endswith(".csv"))
            except zipfile.BadZipFile:
                LOG.warning("Skipping invalid ZIP: %s", path)
    return sources


def read_csv_source(source: tuple[Path, str | None]) -> pd.DataFrame:
    path, member = source
    if member is None:
        return pd.read_csv(path, low_memory=False)
    with zipfile.ZipFile(path) as archive, archive.open(member) as handle:
        return pd.read_csv(handle, low_memory=False)


def source_label(source: tuple[Path, str | None]) -> str:
    path, member = source
    label = path.relative_to(ROOT).as_posix()
    return f"{label}::{member}" if member else label


def normalized_columns(frame: pd.DataFrame) -> dict[str, str]:
    return {str(column).strip().lower(): str(column) for column in frame.columns}


def alias_column(frame: pd.DataFrame, field: str) -> str | None:
    columns = normalized_columns(frame)
    return next((columns[name] for name in TRIP_ALIASES[field] if name in columns), None)


def canonicalize_trip_columns(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for field in REQUIRED_TRIP_FIELDS:
        column = alias_column(frame, field)
        if column is None:
            missing.append(field)
        else:
            mapping[column] = field
    if missing:
        raise ValueError(f"Trip source {source} is missing required fields: {', '.join(missing)}")
    result = frame.rename(columns=mapping).loc[:, list(REQUIRED_TRIP_FIELDS)].copy()
    result["source_asset"] = source
    return result


def load_trips() -> tuple[pd.DataFrame, list[str]]:
    frames: list[pd.DataFrame] = []
    assets: list[str] = []
    for source in csv_sources(DIVVY_DIR):
        sample = read_csv_source(source)
        if alias_column(sample, "started_at") is None:
            continue
        label = source_label(source)
        frames.append(canonicalize_trip_columns(sample, label))
        assets.append(label)
        LOG.info("Loaded Divvy asset %s: %s rows", label, f"{len(sample):,}")
    if not frames:
        raise FileNotFoundError(f"No Divvy trip CSV/ZIP assets found below {DIVVY_DIR}")
    trips = pd.concat(frames, ignore_index=True)
    for column in ("start_lat", "start_lng", "end_lat", "end_lng"):
        trips[column] = pd.to_numeric(trips[column], errors="coerce")
    for column in ("ride_id", "rideable_type", "start_station_name", "start_station_id",
                   "end_station_name", "end_station_id", "member_casual"):
        trips[column] = trips[column].astype("string").str.strip()
        trips.loc[trips[column].eq(""), column] = pd.NA
    return trips, assets


def parse_chicago_time(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, errors="coerce", format="mixed")
    if is_datetime64_any_dtype(parsed.dtype):
        if parsed.dt.tz is None:
            return parsed.dt.tz_localize("America/Chicago", ambiguous="NaT", nonexistent="shift_forward")
        return parsed.dt.tz_convert("America/Chicago")

    def convert_one(value: object) -> pd.Timestamp:
        try:
            stamp = pd.Timestamp(value)
            if pd.isna(stamp):
                return pd.NaT
            if stamp.tzinfo is None:
                return stamp.tz_localize("America/Chicago", ambiguous="NaT", nonexistent="shift_forward")
            return stamp.tz_convert("America/Chicago")
        except (TypeError, ValueError):
            return pd.NaT

    return values.map(convert_one)


def chicago_coordinate_mask(frame: pd.DataFrame) -> pd.Series:
    checks = [
        frame["start_lat"].between(CHICAGO_BOUNDS["lat_min"], CHICAGO_BOUNDS["lat_max"]),
        frame["end_lat"].between(CHICAGO_BOUNDS["lat_min"], CHICAGO_BOUNDS["lat_max"]),
        frame["start_lng"].between(CHICAGO_BOUNDS["lng_min"], CHICAGO_BOUNDS["lng_max"]),
        frame["end_lng"].between(CHICAGO_BOUNDS["lng_min"], CHICAGO_BOUNDS["lng_max"]),
    ]
    valid = checks[0]
    for check in checks[1:]:
        valid &= check
    return valid.fillna(False)


def clean_trips(trips: pd.DataFrame, accessed_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = trips.copy()
    initial_rows = len(work)
    funnel: list[dict[str, object]] = []

    def record(stage: str, before: int, after: int) -> None:
        dropped = before - after
        funnel.append({
            "stage": stage,
            "rows_entering": before,
            "rows_dropped": dropped,
            "drop_pct_of_entering": round(100 * dropped / before, 6) if before else 0.0,
            "drop_pct_of_initial": round(100 * dropped / initial_rows, 6) if initial_rows else 0.0,
            "rows_remaining": after,
            "remaining_pct_of_initial": round(100 * after / initial_rows, 6) if initial_rows else 0.0,
        })

    record("input", initial_rows, initial_rows)
    before = len(work)
    duplicate = work["ride_id"].notna() & work["ride_id"].duplicated(keep="first")
    work = work.loc[~duplicate].copy()
    record("duplicate_ride_id", before, len(work))

    work["started_at"] = parse_chicago_time(work["started_at"])
    work["ended_at"] = parse_chicago_time(work["ended_at"])
    before = len(work)
    invalid_time = work["started_at"].isna() | work["ended_at"].isna()
    work = work.loc[~invalid_time].copy()
    record("invalid_or_missing_timestamp", before, len(work))

    work["duration_min"] = (work["ended_at"] - work["started_at"]).dt.total_seconds() / 60.0
    before = len(work)
    reversed_time = work["ended_at"] < work["started_at"]
    work = work.loc[~reversed_time].copy()
    record("timestamp_reversed", before, len(work))

    before = len(work)
    work = work.loc[work["duration_min"] >= 1.0].copy()
    record("duration_under_1_min", before, len(work))

    before = len(work)
    work = work.loc[work["duration_min"] <= 1440.0].copy()
    record("duration_over_1440_min", before, len(work))

    before = len(work)
    work = work.loc[chicago_coordinate_mask(work)].copy()
    record("coordinates_missing_or_outside_chicago", before, len(work))
    record("clean_output", len(work), len(work))

    funnel_frame = pd.DataFrame(funnel)
    funnel_frame["data_source"] = SOURCE
    funnel_frame["version_accessed_date"] = accessed_date
    return work.reset_index(drop=True), funnel_frame


def valid_station_observations(trips: pd.DataFrame) -> pd.DataFrame:
    observations: list[pd.DataFrame] = []
    for role in ("start", "end"):
        part = trips[[f"{role}_station_id", f"{role}_station_name", f"{role}_lat", f"{role}_lng"]].copy()
        part.columns = ["station_id", "station_name", "lat", "lng"]
        part["observed_role"] = role
        observations.append(part)
    result = pd.concat(observations, ignore_index=True)
    result["station_id"] = result["station_id"].astype("string").str.strip()
    result["station_name"] = result["station_name"].astype("string").str.strip()
    valid = (
        result["station_id"].notna()
        & result["station_name"].notna()
        & result["lat"].between(CHICAGO_BOUNDS["lat_min"], CHICAGO_BOUNDS["lat_max"])
        & result["lng"].between(CHICAGO_BOUNDS["lng_min"], CHICAGO_BOUNDS["lng_max"])
    )
    result = result.loc[valid].copy()
    result["lat_round4"] = result["lat"].round(4)
    result["lng_round4"] = result["lng"].round(4)
    result["station_name_normalized"] = (
        result["station_name"].str.lower().str.replace(r"\s+", " ", regex=True).str.strip()
    )
    return result


def haversine_distance_m(lat: pd.Series, lng: pd.Series, center_lat: float, center_lng: float) -> np.ndarray:
    lat1 = np.radians(lat.to_numpy(dtype=float))
    lng1 = np.radians(lng.to_numpy(dtype=float))
    lat2 = np.radians(center_lat)
    lng2 = np.radians(center_lng)
    delta_lat = lat1 - lat2
    delta_lng = lng1 - lng2
    a = np.sin(delta_lat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(delta_lng / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def harmonize_stations(
    trips: pd.DataFrame, accessed_date: str
) -> tuple[pd.DataFrame, dict[str, object]]:
    observations = valid_station_observations(trips)
    if observations.empty:
        raise ValueError("No valid station observations are available for harmonization")

    coordinates = observations[["lat_round4", "lng_round4"]].drop_duplicates().reset_index(drop=True)
    radians = np.radians(coordinates[["lat_round4", "lng_round4"]].to_numpy())
    coordinates["cluster_label"] = DBSCAN(
        eps=50.0 / EARTH_RADIUS_M,
        min_samples=1,
        metric="haversine",
        algorithm="ball_tree",
    ).fit_predict(radians)

    cluster_rows: list[dict[str, object]] = []
    for label, group in coordinates.groupby("cluster_label", sort=True):
        center_lat = float(group["lat_round4"].mean())
        center_lng = float(group["lng_round4"].mean())
        signature = "|".join(
            f"{lat:.4f},{lng:.4f}"
            for lat, lng in sorted(zip(group["lat_round4"], group["lng_round4"]))
        )
        canonical_id = "CAN_" + hashlib.sha1(signature.encode("utf-8")).hexdigest()[:10].upper()
        for index in group.index:
            cluster_rows.append({
                "lat_round4": coordinates.at[index, "lat_round4"],
                "lng_round4": coordinates.at[index, "lng_round4"],
                "canonical_station_id": canonical_id,
                "canonical_lat": center_lat,
                "canonical_lng": center_lng,
                "cluster_label": int(label),
            })
    coordinate_map = pd.DataFrame(cluster_rows)
    observations = observations.merge(coordinate_map, on=["lat_round4", "lng_round4"], validate="many_to_one")

    key_columns = ["station_id", "station_name", "station_name_normalized", "lat_round4", "lng_round4",
                   "canonical_station_id", "canonical_lat", "canonical_lng", "cluster_label"]
    mapping = (
        observations.groupby(key_columns, dropna=False, as_index=False)
        .agg(occurrence_count=("observed_role", "size"), observed_roles=("observed_role", lambda x: "|".join(sorted(set(x)))))
    )
    # Compute row-wise because each canonical station has its own centroid.
    lat1 = np.radians(mapping["lat_round4"].to_numpy(float))
    lng1 = np.radians(mapping["lng_round4"].to_numpy(float))
    lat2 = np.radians(mapping["canonical_lat"].to_numpy(float))
    lng2 = np.radians(mapping["canonical_lng"].to_numpy(float))
    a = np.sin((lat1 - lat2) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng1 - lng2) / 2) ** 2
    mapping["distance_to_canonical_m"] = 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))

    name_location_counts = mapping.groupby("station_name_normalized")["canonical_station_id"].transform("nunique")
    location_id_counts = mapping.groupby("canonical_station_id")["station_id"].transform("nunique")
    id_location_counts = mapping.groupby("station_id")["canonical_station_id"].transform("nunique")
    mapping["same_name_different_location_flag"] = name_location_counts.gt(1)
    mapping["same_location_different_id_flag"] = location_id_counts.gt(1)
    mapping["station_id_multiple_locations_flag"] = id_location_counts.gt(1)

    meaningful_ids = mapping["station_id"].dropna().astype(str)
    name_drift_ids = set(mapping.loc[mapping["same_name_different_location_flag"], "station_id"].dropna().astype(str))
    location_drift_ids = set(mapping.loc[mapping["same_location_different_id_flag"], "station_id"].dropna().astype(str))
    id_reuse_ids = set(mapping.loc[mapping["station_id_multiple_locations_flag"], "station_id"].dropna().astype(str))
    same_name_groups = int(mapping.loc[mapping["same_name_different_location_flag"], "station_name_normalized"].nunique())
    same_location_groups = int(mapping.loc[mapping["same_location_different_id_flag"], "canonical_station_id"].nunique())
    max_distance = float(mapping["distance_to_canonical_m"].max())
    if max_distance > 50.0001:
        LOG.warning("DBSCAN chain produced a point %.2f m from its cluster centroid", max_distance)

    summary: dict[str, object] = {
        "valid_station_observations": int(len(observations)),
        "historical_station_ids": int(meaningful_ids.nunique()),
        "station_name_coordinate_combinations": int(len(mapping)),
        "unique_rounded_coordinates": int(len(coordinates)),
        "canonical_stations": int(mapping["canonical_station_id"].nunique()),
        "same_name_different_location_names": same_name_groups,
        "historical_ids_in_same_name_different_location": len(name_drift_ids),
        "same_location_different_id_clusters": same_location_groups,
        "historical_ids_in_same_location_different_id": len(location_drift_ids),
        "historical_ids_reused_across_locations": len(id_reuse_ids),
        "historical_ids_with_either_name_or_location_drift": len(name_drift_ids | location_drift_ids),
        "max_coordinate_to_canonical_distance_m": round(max_distance, 3),
        "clustering_radius_m": 50,
    }
    mapping["data_source"] = SOURCE
    mapping["version_accessed_date"] = accessed_date
    mapping = mapping.sort_values(["canonical_station_id", "station_id", "station_name", "lat_round4", "lng_round4"])
    return mapping.reset_index(drop=True), summary


def attach_start_canonical(trips: pd.DataFrame, station_map: pd.DataFrame) -> pd.DataFrame:
    work = trips.copy()
    work["lat_round4"] = work["start_lat"].round(4)
    work["lng_round4"] = work["start_lng"].round(4)
    lookup_columns = ["station_id", "station_name", "lat_round4", "lng_round4", "canonical_station_id",
                      "canonical_lat", "canonical_lng"]
    lookup = station_map[lookup_columns].drop_duplicates(
        ["station_id", "station_name", "lat_round4", "lng_round4"]
    )
    work = work.merge(
        lookup,
        left_on=["start_station_id", "start_station_name", "lat_round4", "lng_round4"],
        right_on=["station_id", "station_name", "lat_round4", "lng_round4"],
        how="left",
        validate="many_to_one",
    ).drop(columns=["station_id", "station_name"])
    missing = int(work["canonical_station_id"].isna().sum())
    if missing:
        raise ValueError(f"Station harmonization failed for {missing} cleaned trip rows")
    return work


def weather_column(frame: pd.DataFrame, name: str) -> str | None:
    columns = normalized_columns(frame)
    if name.lower() in columns:
        return columns[name.lower()]
    pattern = re.compile(rf"^{re.escape(name)}(?:_|\b)", re.IGNORECASE)
    return next((str(column) for column in frame.columns if pattern.match(str(column).strip())), None)


def load_weather() -> tuple[pd.DataFrame, list[str]]:
    frames: list[pd.DataFrame] = []
    assets: list[str] = []
    needed = ("STATION", "DATE", *WEATHER_METRICS)
    for source in csv_sources(NOAA_DIR):
        frame = read_csv_source(source)
        station_column = weather_column(frame, "STATION")
        date_column = weather_column(frame, "DATE")
        if station_column is None or date_column is None:
            continue
        mapping: dict[str, str] = {station_column: "STATION", date_column: "DATE"}
        missing: list[str] = []
        for metric in WEATHER_METRICS:
            column = weather_column(frame, metric)
            if column is None:
                missing.append(metric)
            else:
                mapping[column] = metric
        if missing:
            raise ValueError(f"NOAA source {source_label(source)} is missing: {', '.join(missing)}")
        part = frame.rename(columns=mapping).loc[:, list(needed)].copy()
        part["source_asset"] = source_label(source)
        frames.append(part)
        assets.append(source_label(source))
        LOG.info("Loaded NOAA asset %s: %s rows", source_label(source), f"{len(part):,}")
    if not frames:
        raise FileNotFoundError(f"No NOAA daily CSV/ZIP assets found below {NOAA_DIR}")
    weather = pd.concat(frames, ignore_index=True)
    weather["STATION"] = weather["STATION"].astype("string").str.strip()
    weather["DATE"] = pd.to_datetime(weather["DATE"], errors="coerce").dt.normalize()
    for metric in WEATHER_METRICS:
        weather[metric] = pd.to_numeric(weather[metric], errors="coerce")
    weather = weather.loc[weather["STATION"].isin([OHARE, MIDWAY])].copy()
    if weather.empty or OHARE not in set(weather["STATION"]):
        raise ValueError(f"Primary NOAA station {OHARE} is absent")
    return weather, assets


def first_valid(values: pd.Series) -> float:
    valid = values.dropna()
    return float(valid.iloc[0]) if not valid.empty else np.nan


def detect_weather_units(weather: pd.DataFrame, requested: str) -> str:
    if requested != "auto":
        return requested
    temperatures = pd.concat([weather["TMAX"], weather["TMIN"]]).dropna()
    if temperatures.empty:
        raise ValueError("Cannot infer NOAA units because TMAX/TMIN contain no observations")
    # Chicago temperatures above 60 or below -65 are implausible in Celsius but normal in Fahrenheit.
    detected = "standard" if temperatures.max() > 60 or temperatures.min() < -65 else "metric"
    LOG.info("Auto-detected NOAA input units as %s", detected)
    return detected


def convert_weather_to_metric(weather: pd.DataFrame, unit_system: str) -> pd.DataFrame:
    result = weather.copy()
    if unit_system == "standard":
        for metric in ("TMAX", "TMIN"):
            result[metric] = (result[metric] - 32.0) * 5.0 / 9.0
        for metric in ("PRCP", "SNWD", "SNOW"):
            result[metric] = result[metric] * 25.4
        result["AWND"] = result["AWND"] * 0.44704
    return result


def weather_regime(frame: pd.DataFrame) -> pd.Series:
    conditions = [
        frame["SNOW_mm"].gt(0)
        | (frame["SNWD_mm"].gt(0) & frame["TMIN_C"].le(0))
        | (frame["PRCP_mm"].gt(0) & frame["TMIN_C"].le(0)),
        frame["AWND_mps"].ge(8.0),
        frame["PRCP_mm"].gt(0),
        frame["TMAX_C"].lt(10.0),
    ]
    return pd.Series(
        np.select(conditions, ["Snowy/Icy", "High_Wind", "Rainy", "Cold/Dry"], default="Clear/Warm"),
        index=frame.index,
        dtype="string",
    )


def standardize_weather(
    raw: pd.DataFrame, requested_units: str
) -> tuple[pd.DataFrame, dict[str, object]]:
    invalid_dates = int(raw["DATE"].isna().sum())
    raw = raw.loc[raw["DATE"].notna()].copy()
    duplicate_station_dates = int(raw.duplicated(["STATION", "DATE"]).sum())
    collapsed = raw.groupby(["STATION", "DATE"], as_index=False).agg({metric: first_valid for metric in WEATHER_METRICS})
    unit_system = detect_weather_units(collapsed, requested_units)
    metric = convert_weather_to_metric(collapsed, unit_system)

    bounds = {
        "PRCP": (0.0, 600.0), "SNWD": (0.0, 5000.0), "SNOW": (0.0, 1500.0),
        "AWND": (0.0, 75.0), "TMAX": (-65.0, 60.0), "TMIN": (-65.0, 60.0),
    }
    violations: dict[str, int] = {}
    for name, (low, high) in bounds.items():
        values = metric[name]
        violations[name] = int((values.notna() & ~values.between(low, high)).sum())
    inverted_temperature_rows = int(
        (metric["TMAX"].notna() & metric["TMIN"].notna() & metric["TMAX"].lt(metric["TMIN"])).sum()
    )
    if sum(violations.values()) or inverted_temperature_rows:
        raise ValueError(
            "NOAA metric integrity check failed: "
            f"range violations={violations}, TMAX<TMIN rows={inverted_temperature_rows}"
        )

    primary = metric.loc[metric["STATION"].eq(OHARE)].set_index("DATE")
    backup = metric.loc[metric["STATION"].eq(MIDWAY)].set_index("DATE")
    all_dates = primary.index.union(backup.index).sort_values()
    weather = pd.DataFrame(index=all_dates)
    imputed_counts: dict[str, int] = {}
    remaining_missing: dict[str, int] = {}
    for name in WEATHER_METRICS:
        primary_values = primary[name].reindex(all_dates)
        backup_values = backup[name].reindex(all_dates)
        imputed = primary_values.isna() & backup_values.notna()
        weather[name] = primary_values.fillna(backup_values)
        weather[f"{name}_imputed_from_midway"] = imputed
        imputed_counts[name] = int(imputed.sum())
        remaining_missing[name] = int(weather[name].isna().sum())
    weather = weather.reset_index(names="trip_date")
    weather = weather.rename(columns={
        "PRCP": "PRCP_mm", "SNWD": "SNWD_mm", "SNOW": "SNOW_mm", "AWND": "AWND_mps",
        "TMAX": "TMAX_C", "TMIN": "TMIN_C",
    })
    weather["T_RANGE_C"] = weather["TMAX_C"] - weather["TMIN_C"]
    weather["weather_regime"] = weather_regime(weather)
    summary: dict[str, object] = {
        "input_unit_system": unit_system,
        "invalid_date_rows": invalid_dates,
        "duplicate_station_date_rows_collapsed": duplicate_station_dates,
        "ohare_dates": int(len(primary)),
        "midway_dates": int(len(backup)),
        "combined_dates": int(len(weather)),
        "imputed_from_midway_by_metric": imputed_counts,
        "remaining_missing_by_metric_all_dates": remaining_missing,
        "metric_range_violations": violations,
        "tmax_below_tmin_rows": inverted_temperature_rows,
    }
    return weather, summary


def temporal_regime(year: pd.Series) -> pd.Series:
    return pd.Series(
        np.select(
            [year.le(2019), year.eq(2020), year.between(2021, 2022), year.ge(2023)],
            ["2019_pre_covid", "2020_covid_shock", "2021_2022_recovery", "2023_present"],
            default="unknown",
        ),
        index=year.index,
        dtype="string",
    )


def aggregate_panels(
    trips: pd.DataFrame,
    weather: pd.DataFrame,
    accessed_date: str,
    allow_incomplete_weather: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    work = trips.copy()
    work["trip_date"] = work["started_at"].dt.tz_localize(None).dt.normalize()
    work["trip_hour"] = work["started_at"].dt.floor("h")

    daily_keys = ["canonical_station_id", "trip_date", "member_casual", "rideable_type"]
    daily = (
        work.groupby(daily_keys, dropna=False, as_index=False)
        .agg(
            trip_count=("ride_id", "size"),
            duration_mean_min=("duration_min", "mean"),
            duration_median_min=("duration_min", "median"),
            duration_total_min=("duration_min", "sum"),
            canonical_lat=("canonical_lat", "first"),
            canonical_lng=("canonical_lng", "first"),
        )
    )
    hourly = (
        work.groupby(["canonical_station_id", "trip_hour"], as_index=False)
        .agg(
            trip_count=("ride_id", "size"),
            duration_mean_min=("duration_min", "mean"),
            duration_median_min=("duration_min", "median"),
            duration_total_min=("duration_min", "sum"),
            member_trip_count=("member_casual", lambda x: int(x.eq("member").sum())),
            casual_trip_count=("member_casual", lambda x: int(x.eq("casual").sum())),
            canonical_lat=("canonical_lat", "first"),
            canonical_lng=("canonical_lng", "first"),
        )
    )
    hourly["trip_date"] = hourly["trip_hour"].dt.tz_localize(None).dt.normalize()
    daily = daily.merge(weather, on="trip_date", how="left", validate="many_to_one", indicator="weather_join_status")
    hourly = hourly.merge(weather, on="trip_date", how="left", validate="many_to_one", indicator="weather_join_status")

    required_weather = ["PRCP_mm", "SNWD_mm", "SNOW_mm", "AWND_mps", "TMAX_C", "TMIN_C"]
    unmatched_daily_rows = int(daily["weather_join_status"].ne("both").sum())
    incomplete_daily_rows = int(daily[required_weather].isna().any(axis=1).sum())
    trip_dates = set(work["trip_date"].dropna())
    available_dates = set(weather["trip_date"].dropna())
    missing_trip_dates = sorted(pd.Timestamp(value).date().isoformat() for value in trip_dates - available_dates)
    if (unmatched_daily_rows or incomplete_daily_rows) and not allow_incomplete_weather:
        raise ValueError(
            "Weather completeness gate failed for the Divvy date range: "
            f"unmatched daily panel rows={unmatched_daily_rows}, rows with missing metrics={incomplete_daily_rows}, "
            f"missing dates={missing_trip_dates[:10]}. Use --allow-incomplete-weather only after review."
        )

    for panel in (daily, hourly):
        panel["covid_period_flag"] = panel["trip_date"].dt.year.eq(2020)
        panel["temporal_regime"] = temporal_regime(panel["trip_date"].dt.year)
        panel["data_source"] = SOURCE
        panel["version_accessed_date"] = accessed_date
        panel.drop(columns="weather_join_status", inplace=True)

    summary: dict[str, object] = {
        "clean_trip_rows": int(len(work)),
        "trip_dates": int(len(trip_dates)),
        "daily_panel_rows": int(len(daily)),
        "hourly_feature_rows": int(len(hourly)),
        "daily_panel_unmatched_weather_rows": unmatched_daily_rows,
        "daily_panel_rows_with_incomplete_weather": incomplete_daily_rows,
        "trip_dates_missing_weather": missing_trip_dates,
        "covid_trip_rows": int(work["trip_date"].dt.year.eq(2020).sum()),
    }
    return daily, hourly, summary


def add_provenance(frame: pd.DataFrame, accessed_date: str) -> pd.DataFrame:
    result = frame.copy()
    result["data_source"] = SOURCE
    result["version_accessed_date"] = accessed_date
    return result


def write_outputs(
    funnel: pd.DataFrame,
    station_map: pd.DataFrame,
    weather: pd.DataFrame,
    daily: pd.DataFrame,
    hourly: pd.DataFrame,
    summary: dict[str, object],
    accessed_date: str,
) -> None:
    funnel.to_csv(TABLE_DIR / "01_cleaning_funnel.csv", index=False)
    station_map.to_csv(TABLE_DIR / "01_station_harmonization_map.csv", index=False)
    add_provenance(weather, accessed_date).to_csv(TABLE_DIR / "01_weather_daily_standardized.csv", index=False)
    daily.to_parquet(PROCESSED_DIR / "station_daily_weather_panel.parquet", index=False)
    hourly.to_parquet(PROCESSED_DIR / "station_hourly_weather_features.parquet", index=False)
    with (TABLE_DIR / "01_clean_and_merge_metrics.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = parse_args()
    setup_dirs()
    raw_trips, divvy_assets = load_trips()
    clean, funnel = clean_trips(raw_trips, args.accessed_date)
    station_map, station_summary = harmonize_stations(raw_trips, args.accessed_date)
    clean = attach_start_canonical(clean, station_map)
    raw_weather, noaa_assets = load_weather()
    weather, weather_summary = standardize_weather(raw_weather, args.noaa_input_units)
    daily, hourly, panel_summary = aggregate_panels(
        clean, weather, args.accessed_date, args.allow_incomplete_weather
    )
    total_dropped = int(len(raw_trips) - len(clean))
    summary: dict[str, object] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "accessed_date": args.accessed_date,
        "data_source": SOURCE,
        "input_assets": {"divvy": divvy_assets, "noaa": noaa_assets},
        "integrity_gate_status": "PASS",
        "trip_cleaning": {
            "input_rows": int(len(raw_trips)),
            "clean_rows": int(len(clean)),
            "total_dropped": total_dropped,
            "total_drop_pct": round(100 * total_dropped / len(raw_trips), 6) if len(raw_trips) else 0.0,
            "funnel": funnel.drop(columns=["data_source", "version_accessed_date"]).to_dict(orient="records"),
        },
        "station_harmonization": station_summary,
        "weather_standardization": weather_summary,
        "panels": panel_summary,
        "outputs": {
            "cleaning_funnel": "outputs/tables/01_cleaning_funnel.csv",
            "station_harmonization_map": "outputs/tables/01_station_harmonization_map.csv",
            "standardized_weather": "outputs/tables/01_weather_daily_standardized.csv",
            "station_daily_panel": "data/processed/station_daily_weather_panel.parquet",
            "station_hourly_features": "data/processed/station_hourly_weather_features.parquet",
        },
    }
    write_outputs(funnel, station_map, weather, daily, hourly, summary, args.accessed_date)
    LOG.info(
        "PASS | trips %s -> %s; canonical stations=%s; daily rows=%s; hourly rows=%s",
        f"{len(raw_trips):,}", f"{len(clean):,}", station_summary["canonical_stations"],
        f"{len(daily):,}", f"{len(hourly):,}",
    )
    LOG.info("Station drift summary: %s", json.dumps(station_summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
