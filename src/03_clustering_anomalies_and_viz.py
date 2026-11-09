#!/usr/bin/env python3
"""Station clustering, station-day anomalies, and PPT-ready figures.

Consumes only aggregate outputs from 01_clean_and_merge.py; raw Divvy rows are
never read or redistributed.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import warnings
from datetime import date, datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.colors import TwoSlopeNorm
from scipy.optimize import linear_sum_assignment
from scipy.stats import t
from sklearn.cluster import KMeans
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import silhouette_score
from sklearn.mixture import GaussianMixture
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[1]
DAILY_DEFAULT = ROOT / "data/processed/station_daily_weather_panel.parquet"
HOURLY_DEFAULT = ROOT / "data/processed/station_hourly_weather_features.parquet"
TABLE_DIR, FIGURE_DIR = ROOT / "outputs/tables", ROOT / "outputs/figures"
SOURCE = "Divvy System Data & NOAA NCEI"
LOOP = (41.8837, -87.6324)
EARTH_RADIUS_KM = 6371.0088
LAKEFRONT = np.array([
    [42.045, -87.677], [42.000, -87.657], [41.960, -87.642],
    [41.920, -87.630], [41.885, -87.615], [41.840, -87.606],
    [41.790, -87.583], [41.760, -87.558],
])
FEATURES = [
    "weekday_peak_share", "weekend_weekday_ratio", "member_share", "electric_share",
    "distance_loop_km", "distance_lakefront_km", "rain_retention_ratio",
    "cold_wind_retention_ratio",
]
FEATURE_LABELS = [
    "Peak\nshare", "Weekend /\nweekday", "Member\nshare", "Electric\nshare",
    "Loop\ndistance", "Lakefront\ndistance", "Rain\nretention", "Cold/wind\nretention",
]
WEATHER = ["PRCP_mm", "SNOW_mm", "SNWD_mm", "AWND_mps", "TMAX_C", "TMIN_C"]
REQ_DAILY = {
    "canonical_station_id", "trip_date", "member_casual", "rideable_type", "trip_count",
    "canonical_lat", "canonical_lng", "temporal_regime", *WEATHER,
}
REQ_HOURLY = {"canonical_station_id", "trip_hour", "trip_date", "trip_count"}
LOG = logging.getLogger("clustering_anomalies_viz")


def args_parser() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-panel", type=Path, default=DAILY_DEFAULT)
    parser.add_argument("--hourly-panel", type=Path, default=HOURLY_DEFAULT)
    parser.add_argument("--accessed-date", default=date.today().isoformat())
    parser.add_argument("--min-station-trips", type=int, default=20)
    parser.add_argument("--thermal-bin-width-c", type=float, default=2.0)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()
    try:
        date.fromisoformat(args.accessed_date)
    except ValueError:
        parser.error("--accessed-date must be YYYY-MM-DD")
    if args.min_station_trips < 1 or args.thermal_bin_width_c <= 0:
        parser.error("minimum trips and bin width must be positive")
    return args


def provenance(frame: pd.DataFrame, accessed: str) -> pd.DataFrame:
    frame = frame.copy()
    frame["data_source"] = SOURCE
    frame["version_accessed_date"] = accessed
    return frame


def load_inputs(daily_path: Path, hourly_path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not daily_path.exists() or not hourly_path.exists():
        raise FileNotFoundError("Run src/01_clean_and_merge.py before this script")
    daily, hourly = pd.read_parquet(daily_path), pd.read_parquet(hourly_path)
    missing = {"daily": sorted(REQ_DAILY - set(daily)), "hourly": sorted(REQ_HOURLY - set(hourly))}
    if missing["daily"] or missing["hourly"]:
        raise ValueError(f"Missing required fields: {missing}")
    daily, hourly = daily.copy(), hourly.copy()
    daily["trip_date"] = pd.to_datetime(daily["trip_date"], errors="coerce").dt.normalize()
    hourly["trip_date"] = pd.to_datetime(hourly["trip_date"], errors="coerce").dt.normalize()
    hourly["trip_hour"] = pd.to_datetime(hourly["trip_hour"], errors="coerce", utc=True).dt.tz_convert("America/Chicago")
    for column in ["trip_count", "canonical_lat", "canonical_lng", *WEATHER]:
        daily[column] = pd.to_numeric(daily[column], errors="coerce")
    hourly["trip_count"] = pd.to_numeric(hourly["trip_count"], errors="coerce")
    if daily[["trip_date", "trip_count", "canonical_lat", "canonical_lng", *WEATHER]].isna().any().any():
        raise ValueError("Daily aggregate input has missing required values")
    if hourly[["trip_date", "trip_hour", "trip_count"]].isna().any().any():
        raise ValueError("Hourly aggregate input has missing required values")
    keys = ["canonical_station_id", "trip_date", "member_casual", "rideable_type"]
    if daily.duplicated(keys).any() or daily["trip_count"].lt(0).any() or hourly["trip_count"].lt(0).any():
        raise ValueError("Duplicate daily keys or negative trip counts detected")
    weather = daily[["trip_date", *WEATHER, "temporal_regime"]].drop_duplicates()
    if weather.duplicated("trip_date").any():
        raise ValueError("Weather differs within a date")
    weather = weather.sort_values("trip_date").reset_index(drop=True)
    complete = pd.date_range(weather.trip_date.min(), weather.trip_date.max(), freq="D")
    if len(complete.difference(pd.DatetimeIndex(weather.trip_date))):
        raise ValueError("Calendar dates are missing; zero-trip and absent days cannot be distinguished")
    LOG.info("Loaded daily=%s, hourly=%s, stations=%s, dates=%s", len(daily), len(hourly), daily.canonical_station_id.nunique(), len(weather))
    return daily, hourly, weather


def station_day_grid(daily: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    stations = daily[["canonical_station_id", "canonical_lat", "canonical_lng"]].drop_duplicates("canonical_station_id")
    counts = daily.groupby(["canonical_station_id", "trip_date"], as_index=False).trip_count.sum()
    grid = pd.MultiIndex.from_product(
        [stations.canonical_station_id, weather.trip_date], names=["canonical_station_id", "trip_date"]
    ).to_frame(index=False)
    grid = grid.merge(stations, on="canonical_station_id", validate="many_to_one")
    grid = grid.merge(counts, on=["canonical_station_id", "trip_date"], how="left", validate="one_to_one")
    grid["trip_count"] = grid.trip_count.fillna(0.0)
    grid = grid.merge(weather, on="trip_date", validate="many_to_one")
    grid["day_of_week"] = grid.trip_date.dt.dayofweek
    grid["is_weekend"] = grid.day_of_week.ge(5)
    return grid


def haversine(lat: pd.Series, lng: pd.Series, target: tuple[float, float]) -> np.ndarray:
    lat1, lng1 = np.radians(lat.to_numpy(float)), np.radians(lng.to_numpy(float))
    lat2, lng2 = map(math.radians, target)
    a = np.sin((lat1-lat2)/2)**2 + np.cos(lat1)*np.cos(lat2)*np.sin((lng1-lng2)/2)**2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def lakefront_distance(lat: pd.Series, lng: pd.Series) -> np.ndarray:
    ref = math.radians(float(LAKEFRONT[:, 0].mean()))
    points = np.c_[lng.to_numpy(float)*111.32*math.cos(ref), lat.to_numpy(float)*110.574]
    line = np.c_[LAKEFRONT[:, 1]*111.32*math.cos(ref), LAKEFRONT[:, 0]*110.574]
    distances = np.full(len(points), np.inf)
    for start, end in zip(line[:-1], line[1:]):
        vector = end-start
        fraction = np.clip(((points-start) @ vector)/(vector @ vector), 0, 1)
        distances = np.minimum(distances, np.linalg.norm(points-(start+fraction[:, None]*vector), axis=1))
    return distances


def retention_ratio(group: pd.DataFrame, event: pd.Series) -> float:
    """Event/non-event demand after shrinkage-normalizing ordinary weekday pattern."""
    overall = max(float(group.trip_count.mean()), .25)
    stats = group.groupby("day_of_week").trip_count.agg(["sum", "count"])
    baseline = (stats["sum"]+2*overall)/(stats["count"]+2)
    normalized = group.trip_count/group.day_of_week.map(baseline).clip(lower=.25)
    event = pd.Series(event, index=group.index).fillna(False).astype(bool)
    if event.sum() < 2 or (~event).sum() < 2:
        return 1.0
    denominator = normalized.loc[~event].mean()
    return float(np.clip(normalized.loc[event].mean()/denominator, .05, 4)) if denominator > 0 else 1.0


def make_station_features(daily, hourly, station_days, minimum):
    totals = daily.groupby("canonical_station_id").trip_count.sum().sort_values(ascending=False)
    active, note = totals[totals.ge(minimum)].index.tolist(), "fixed_minimum_total_trips"
    if len(active) < min(3, len(totals)):
        active, note = totals.head(min(3, len(totals))).index.tolist(), "threshold_fallback_top_stations"
    if len(active) < 2:
        raise ValueError("At least two active stations are required")
    d = daily[daily.canonical_station_id.isin(active)].copy()
    h = hourly[hourly.canonical_station_id.isin(active)].copy()
    sd = station_days[station_days.canonical_station_id.isin(active)].copy()
    frame = d.groupby("canonical_station_id").agg(
        canonical_lat=("canonical_lat", "first"), canonical_lng=("canonical_lng", "first"), total_trips=("trip_count", "sum")
    )
    frame["mean_daily_trips"] = frame.total_trips/sd.trip_date.nunique()
    h["hour"], h["weekday"] = h.trip_hour.dt.hour, h.trip_date.dt.dayofweek.lt(5)
    weekday = h[h.weekday]
    frame["weekday_peak_share"] = weekday.groupby("canonical_station_id").apply(
        lambda x: x.loc[x.hour.isin([7,8,9,16,17,18]), "trip_count"].sum()/max(x.trip_count.sum(), 1), include_groups=False)
    means = sd.groupby(["canonical_station_id", "is_weekend"]).trip_count.mean().unstack().reindex(columns=[False, True], fill_value=0)
    frame["weekend_weekday_ratio"] = means[True]/means[False].clip(lower=.25)
    customer = d.groupby(["canonical_station_id", "member_casual"]).trip_count.sum().unstack(fill_value=0)
    bike = d.groupby(["canonical_station_id", "rideable_type"]).trip_count.sum().unstack(fill_value=0)
    frame["member_share"] = customer.get("member", 0)/customer.sum(axis=1).clip(lower=1)
    frame["electric_share"] = bike.get("electric_bike", 0)/bike.sum(axis=1).clip(lower=1)
    frame["distance_loop_km"] = haversine(frame.canonical_lat, frame.canonical_lng, LOOP)
    frame["distance_lakefront_km"] = lakefront_distance(frame.canonical_lat, frame.canonical_lng)
    wind90, cold25 = float(sd.AWND_mps.quantile(.9)), float(sd.TMAX_C.quantile(.25))
    for station, group in sd.groupby("canonical_station_id"):
        frame.loc[station, "rain_retention_ratio"] = retention_ratio(group, group.PRCP_mm.gt(0))
        frame.loc[station, "cold_wind_retention_ratio"] = retention_ratio(group, group.TMAX_C.le(cold25)|group.AWND_mps.ge(wind90))
    for column in FEATURES:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame[column] = frame[column].fillna(0 if frame[column].isna().all() else frame[column].median())
    summary = {
        "all_stations": len(totals), "active_stations": len(frame), "excluded_low_volume_stations": len(totals)-len(frame),
        "minimum_station_trips": minimum, "filter_note": note, "rain_definition": "PRCP_mm > 0",
        "cold_wind_definition": f"TMAX_C <= sample P25 ({cold25:.3f} C) or AWND_mps >= sample P90 ({wind90:.3f} m/s)",
        "retention_adjustment": "event/non-event ratio after empirical-Bayes weekday normalization",
    }
    return frame.reset_index(), summary


def cluster_names(center: pd.DataFrame) -> dict[int, str]:
    names = ["CBD rigid commuter", "Lakefront leisure", "Community mixed connector", "Campus/park resilient", "E-bike mobility hub", "Weather-sensitive local"]
    z = center
    scores = np.c_[
        1.3*z.weekday_peak_share+z.member_share+.5*z.rain_retention_ratio-z.distance_loop_km,
        1.2*z.weekend_weekday_ratio-z.member_share-1.2*z.distance_lakefront_km,
        -.6*z[FEATURES].abs().sum(axis=1),
        z.rain_retention_ratio+z.cold_wind_retention_ratio+.3*z.electric_share,
        1.4*z.electric_share+.5*z.weekday_peak_share,
        -z.rain_retention_ratio-z.cold_wind_retention_ratio+.3*z.weekend_weekday_ratio,
    ]
    rows, columns = linear_sum_assignment(-scores)
    return {int(z.index[row]): names[column] for row, column in zip(rows, columns)}


def fit_clusters(features, seed, accessed):
    scaler, n = StandardScaler(), len(features)
    matrix = scaler.fit_transform(features[FEATURES])
    candidates, fallback = [k for k in range(3, 7) if k < n], False
    if not candidates:
        candidates, fallback = ([2] if n >= 3 else []), True
    trials, labels_by_key = [], {}
    for k in candidates:
        models = {
            "KMeans": KMeans(n_clusters=k, random_state=seed, n_init=50),
            "GaussianMixture": GaussianMixture(n_components=k, random_state=seed, n_init=20, reg_covar=1e-5),
        }
        for name, model in models.items():
            try:
                labels = model.fit_predict(matrix)
                score = silhouette_score(matrix, labels) if 1 < np.unique(labels).size < n else np.nan
                trials.append({"model": name, "k": k, "silhouette_score": score, "status": "ok" if np.isfinite(score) else "degenerate"})
                if np.isfinite(score):
                    labels_by_key[(name, k)] = labels
            except Exception as error:
                trials.append({"model": name, "k": k, "silhouette_score": np.nan, "status": f"failed:{type(error).__name__}"})
    valid = [row for row in trials if np.isfinite(row["silhouette_score"])]
    if valid:
        best = max(valid, key=lambda row: (row["silhouette_score"], row["model"] == "KMeans"))
        labels = labels_by_key[(best["model"], best["k"])]
        model_name, selected_k, score = best["model"], best["k"], float(best["silhouette_score"])
    else:
        labels, model_name, selected_k, score, fallback = np.arange(n), "deterministic_singletons", n, np.nan, True
    assignments = features.copy()
    assignments["cluster_id"] = labels.astype(int)
    for index, feature in enumerate(FEATURES):
        assignments[f"z_{feature}"] = matrix[:, index]
    center = assignments.groupby("cluster_id")[[f"z_{f}" for f in FEATURES]].mean()
    center.columns = FEATURES
    assignments["cluster_name"] = assignments.cluster_id.map(cluster_names(center))
    profile = assignments.groupby("cluster_id", as_index=False).agg(
        cluster_name=("cluster_name", "first"), station_count=("canonical_station_id", "nunique"),
        total_trips=("total_trips", "sum"), mean_daily_trips=("mean_daily_trips", "mean"),
    )
    for feature in FEATURES:
        raw = assignments.groupby("cluster_id")[feature].mean()
        profile[feature], profile[f"z_{feature}"] = profile.cluster_id.map(raw), profile.cluster_id.map(center[feature])
    profile["selected_model"], profile["selected_k"] = model_name, selected_k
    profile["silhouette_score"], profile["small_sample_fallback"] = score, fallback
    summary = {
        "requested_k_candidates": list(range(3, 7)), "valid_k_candidates": candidates, "selected_model": model_name,
        "selected_k": selected_k, "silhouette_score": score if np.isfinite(score) else None, "small_sample_fallback": fallback,
        "fallback_reason": "No k in 3..6 satisfies k < n_stations; k=2 is an exploratory demonstration." if fallback and n >= 3 else None,
        "trials": [{**row, "silhouette_score": row["silhouette_score"] if np.isfinite(row["silhouette_score"]) else None} for row in trials],
    }
    return provenance(assignments, accessed), provenance(profile, accessed), provenance(pd.DataFrame(trials), accessed), summary


def primary_condition(frame: pd.DataFrame, wind90: float) -> pd.Series:
    return pd.Series(np.select(
        [frame.SNOW_mm.gt(0)|frame.SNWD_mm.gt(0), frame.AWND_mps.ge(wind90), frame.PRCP_mm.ge(5), frame.PRCP_mm.gt(0)],
        ["Snow/snow depth", "Strong wind", "Heavy rain", "Light rain"], default="Clear baseline"), index=frame.index)


def detect_anomalies(station_days, assignments, accessed):
    frame = station_days[station_days.canonical_station_id.isin(assignments.canonical_station_id)].copy()
    frame["day_of_week_label"] = frame.trip_date.dt.day_name()
    frame["day_index"] = (frame.trip_date-frame.trip_date.min()).dt.days
    frame["TMAX_C_sq"] = frame.TMAX_C**2
    categorical = ["canonical_station_id", "day_of_week_label", "temporal_regime"]
    numeric = ["TMAX_C", "TMAX_C_sq", "day_index"]
    transform = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore", drop="first"), categorical),
        ("num", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), numeric),
    ])
    model = Pipeline([("features", transform), ("ridge", Ridge(alpha=2.0))])
    target = np.log1p(frame.trip_count.to_numpy(float))
    model.fit(frame[categorical+numeric], target)
    predicted_log = model.predict(frame[categorical+numeric])
    frame["expected_trip_count"] = np.expm1(predicted_log).clip(min=.05)
    frame["log_residual"] = target-predicted_log

    def robust_z(values):
        center = float(values.median())
        scale = 1.4826*float(np.median(np.abs(values-center)))
        if not np.isfinite(scale) or scale < 1e-6:
            scale = float(values.std(ddof=1))
        return (values-center)/(scale if np.isfinite(scale) and scale >= 1e-6 else 1)

    frame["residual_z"] = frame.groupby("canonical_station_id").log_residual.transform(robust_z)
    frame["absolute_residual_z"] = frame.residual_z.abs()
    wind90 = float(frame.AWND_mps.quantile(.9))
    frame["weather_condition"] = primary_condition(frame, wind90)
    frame["is_adverse_weather"] = frame.weather_condition.ne("Clear baseline")
    frame["anomaly_direction"] = np.where(frame.residual_z.ge(0), "surge", "drop")
    frame = frame.merge(assignments[["canonical_station_id", "cluster_id", "cluster_name"]], on="canonical_station_id", validate="many_to_one")
    frame["observed_expected_ratio"] = frame.trip_count/frame.expected_trip_count
    ranked = frame.sort_values(["absolute_residual_z", "trip_date"], ascending=[False, True]).reset_index(drop=True)
    ranked["anomaly_rank"] = np.arange(1, len(ranked)+1)
    top = ranked.head(20).copy()
    top["trip_date"] = top.trip_date.dt.strftime("%Y-%m-%d")
    columns = [
        "anomaly_rank", "canonical_station_id", "trip_date", "cluster_id", "cluster_name", "canonical_lat", "canonical_lng",
        "trip_count", "expected_trip_count", "observed_expected_ratio", "log_residual", "residual_z", "absolute_residual_z",
        "anomaly_direction", "weather_condition", "is_adverse_weather", *WEATHER, "temporal_regime",
    ]
    summary = {
        "model": "Ridge on log1p station-day demand", "regular_pattern_controls": categorical+numeric,
        "rows_scored": len(frame), "station_dates": frame.trip_date.nunique(), "top_events_returned": len(top),
        "wind_p90_mps": wind90, "residual_standardization": "within-station median/MAD Z-score (SD fallback)",
    }
    return provenance(top[columns], accessed), ranked, summary


def customer_day_grid(daily, weather):
    customers = [x for x in ["member", "casual"] if x in set(daily.member_casual)]
    grid = pd.MultiIndex.from_product([weather.trip_date, customers], names=["trip_date", "member_casual"]).to_frame(index=False)
    counts = daily.groupby(["trip_date", "member_casual"], as_index=False).trip_count.sum()
    grid = grid.merge(counts, on=["trip_date", "member_casual"], how="left", validate="one_to_one")
    grid["trip_count"] = grid.trip_count.fillna(0)
    return grid.merge(weather, on="trip_date", validate="many_to_one")


def temperature_response(daily, weather, width, accessed):
    frame = customer_day_grid(daily, weather)
    low, high = math.floor(frame.TMAX_C.min()/width)*width, math.ceil(frame.TMAX_C.max()/width)*width
    if high <= frame.TMAX_C.max(): high += width
    frame["temperature_bin"] = pd.cut(frame.TMAX_C, np.arange(low, high+width*.5, width), right=False, include_lowest=True)
    result = frame.groupby(["member_casual", "temperature_bin"], observed=True, as_index=False).agg(
        bin_days=("trip_date", "nunique"), tmax_mean_c=("TMAX_C", "mean"), mean_daily_trips=("trip_count", "mean"), daily_trips_sd=("trip_count", "std"))
    result["ci_half_width"] = result.apply(lambda row: float(t.ppf(.975, row.bin_days-1))*row.daily_trips_sd/math.sqrt(row.bin_days) if row.bin_days >= 2 and pd.notna(row.daily_trips_sd) else 0, axis=1)
    result["ci_low"] = (result.mean_daily_trips-result.ci_half_width).clip(lower=0)
    result["ci_high"] = result.mean_daily_trips+result.ci_half_width
    result["temperature_bin"] = result.temperature_bin.astype(str)
    bends = {}
    for customer, group in result.groupby("member_casual"):
        group = group.sort_values("tmax_mean_c")
        if len(group) >= 3:
            slopes = np.gradient(group.mean_daily_trips, group.tmax_mean_c)
            curve = np.abs(np.gradient(slopes, group.tmax_mean_c)); curve[0] = curve[-1] = -np.inf
            bends[str(customer)] = float(group.iloc[int(np.argmax(curve))].tmax_mean_c)
        else:
            bends[str(customer)] = None
    return provenance(result, accessed), bends


def segment_retention(daily, weather, accessed):
    segments = pd.MultiIndex.from_product([["member", "casual"], ["classic_bike", "electric_bike"]], names=["member_casual", "rideable_type"]).to_frame(index=False)
    dates, segments = weather[["trip_date"]].assign(key=1), segments.assign(key=1)
    grid = dates.merge(segments, on="key").drop(columns="key")
    counts = daily.groupby(["trip_date", "member_casual", "rideable_type"], as_index=False).trip_count.sum()
    grid = grid.merge(counts, on=["trip_date", "member_casual", "rideable_type"], how="left", validate="one_to_one")
    grid["trip_count"] = grid.trip_count.fillna(0); grid = grid.merge(weather, on="trip_date", validate="many_to_one")
    grid["day_of_week"] = grid.trip_date.dt.dayofweek
    wind90 = float(weather.AWND_mps.quantile(.9))
    conditions = {
        "Clear baseline": lambda x: x.PRCP_mm.eq(0)&x.SNOW_mm.eq(0)&x.SNWD_mm.eq(0)&x.AWND_mps.lt(wind90),
        "Light rain": lambda x: x.PRCP_mm.gt(0)&x.PRCP_mm.lt(5), "Heavy rain": lambda x: x.PRCP_mm.ge(5),
        "Strong wind": lambda x: x.AWND_mps.ge(wind90), "Snow/depth": lambda x: x.SNOW_mm.gt(0)|x.SNWD_mm.gt(0),
    }
    rows = []
    for (member, bike), group in grid.groupby(["member_casual", "rideable_type"], sort=False):
        overall = max(float(group.trip_count.mean()), .25)
        stats = group.groupby("day_of_week").trip_count.agg(["sum", "count"])
        expected = (stats["sum"]+2*overall)/(stats["count"]+2)
        normalized = group.trip_count/group.day_of_week.map(expected).clip(lower=.25)
        for name, condition in conditions.items():
            mask = condition(group)
            ratio = 1.0 if name == "Clear baseline" else float(normalized[mask].mean()) if mask.any() else np.nan
            rows.append({"segment": f"{member.title()}-{'Electric' if bike == 'electric_bike' else 'Classic'}", "member_casual": member, "rideable_type": bike, "weather_condition": name, "retention_ratio": ratio, "event_days": int(mask.sum()), "estimation_note": "weekday-normalized demand index; clear baseline fixed to 1.0"})
    counts_by_condition = {name: int(fn(weather).sum()) for name, fn in conditions.items()}
    summary = {"wind_p90_mps": wind90, "condition_day_counts": counts_by_condition, "interpretation": "Exploratory weekday-normalized index; overlapping conditions are not causal effects."}
    return provenance(pd.DataFrame(rows), accessed), summary


def plot_style():
    sns.set_theme(style="whitegrid", context="talk")
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "axes.titleweight": "bold", "axes.labelsize": 12,
        "axes.titlesize": 15, "legend.fontsize": 9, "xtick.labelsize": 10, "ytick.labelsize": 10,
        "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    })


def plot_weather(thermal, bends, retention, accessed, output):
    plot_style()
    fig, axes = plt.subplots(1, 2, figsize=(18, 8), constrained_layout=True)
    fig.get_layout_engine().set(rect=(0, .075, 1, .89))
    colors = {"member": "#0072B2", "casual": "#D55E00"}
    for customer, group in thermal.groupby("member_casual"):
        group = group.sort_values("tmax_mean_c")
        x, y = group.tmax_mean_c.to_numpy(float), group.mean_daily_trips.to_numpy(float)
        axes[0].plot(x, y, "o-", lw=2.8, label=customer.title(), color=colors[customer])
        axes[0].fill_between(x, group.ci_low.to_numpy(float), group.ci_high.to_numpy(float), color=colors[customer], alpha=.16)
        bend = bends.get(str(customer))
        if bend is not None:
            point = group.iloc[(group.tmax_mean_c-bend).abs().argmin()]
            axes[0].annotate(f"{customer.title()} marginal bend\n~{bend:.1f}°C (exploratory)",
                xy=(point.tmax_mean_c, point.mean_daily_trips), xytext=(10, 18), textcoords="offset points",
                fontsize=9, color=colors[customer], arrowprops={"arrowstyle": "->", "color": colors[customer]})
    axes[0].set(title="A. Nonlinear temperature response", xlabel="Daily maximum temperature, TMAX (°C)", ylabel="Mean daily trips")
    axes[0].legend(frameon=False, title="Customer type"); sns.despine(ax=axes[0])

    segments = ["Member-Classic", "Member-Electric", "Casual-Classic", "Casual-Electric"]
    conditions = ["Clear baseline", "Light rain", "Heavy rain", "Strong wind", "Snow/depth"]
    heat = retention.pivot(index="segment", columns="weather_condition", values="retention_ratio").reindex(index=segments, columns=conditions)
    support = retention.pivot(index="segment", columns="weather_condition", values="event_days").reindex(index=segments, columns=conditions)
    annotations = heat.copy().astype(object)
    for row in heat.index:
        for column in heat.columns:
            value = heat.loc[row, column]
            annotations.loc[row, column] = "NA" if pd.isna(value) else f"{100*value:.0f}%\n(n={int(support.loc[row, column])})"
    finite = heat.to_numpy(float)[np.isfinite(heat.to_numpy(float))]
    vmin, vmax = min(.5, float(finite.min())), max(1.5, float(finite.max()))
    sns.heatmap(heat, ax=axes[1], cmap="vlag", norm=TwoSlopeNorm(vmin=vmin, vcenter=1, vmax=vmax),
        annot=annotations, fmt="", linewidths=1, linecolor="white", mask=heat.isna(),
        cbar_kws={"label": "Weekday-adjusted demand index (1.0 = expected)"})
    axes[1].set(title="B. Customer × bike resilience under adverse weather", xlabel="Weather condition (conditions may overlap)", ylabel="")
    axes[1].tick_params(axis="x", rotation=22); axes[1].tick_params(axis="y", rotation=0)
    fig.suptitle("Structural divergence under weather shocks: nonlinear temperature response and demand resilience", fontsize=20, fontweight="bold")
    fig.text(.5, .006, f"Data Source: {SOURCE} | Version/Accessed Date: {accessed} | 95% t intervals; exploratory EDA, not causal estimates", ha="center", fontsize=9, color="#555555")
    fig.savefig(output, dpi=300, bbox_inches="tight"); plt.close(fig)


def plot_clusters(assignments, profiles, anomalies, accessed, output):
    plot_style()
    fig, axes = plt.subplots(1, 2, figsize=(19, 8), constrained_layout=True)
    fig.get_layout_engine().set(rect=(0, .075, 1, .89))
    cluster_ids = sorted(assignments.cluster_id.unique())
    palette = dict(zip(cluster_ids, sns.color_palette("colorblind", len(cluster_ids))))
    offsets = [(5, 10), (5, -13), (5, 24), (-72, 10), (-72, -13)]
    label_offsets = {
        station_id: offsets[index % len(offsets)]
        for index, station_id in enumerate(sorted(assignments.canonical_station_id))
    }
    low, high = assignments.mean_daily_trips.min(), assignments.mean_daily_trips.max()
    sizes = 100+500*(assignments.mean_daily_trips-low)/(high-low) if high > low else pd.Series(300., index=assignments.index)
    for cluster, group in assignments.groupby("cluster_id"):
        axes[0].scatter(group.canonical_lng, group.canonical_lat, s=sizes.loc[group.index], c=[palette[cluster]], alpha=.86,
            edgecolor="white", linewidth=1.2, label=group.cluster_name.iloc[0], zorder=3)
        if len(assignments) <= 30:
            for row in group.itertuples():
                axes[0].annotate(row.canonical_station_id.replace("CAN_", ""), (row.canonical_lng, row.canonical_lat), xytext=label_offsets[row.canonical_station_id], textcoords="offset points", fontsize=8)
    axes[0].plot(LAKEFRONT[:,1], LAKEFRONT[:,0], color="#56B4E9", lw=4, alpha=.75, label="Approx. Lakefront Trail")
    axes[0].scatter([LOOP[1]], [LOOP[0]], marker="*", s=260, color="#E69F00", edgecolor="black", lw=.8, label="The Loop", zorder=5)
    axes[0].set(title="A. Spatial station-function clusters", xlabel="Longitude", ylabel="Latitude")
    axes[0].legend(frameon=True, fontsize=8, loc="best"); axes[0].set_aspect(1/math.cos(math.radians(assignments.canonical_lat.mean()))); sns.despine(ax=axes[0])

    x = np.arange(len(FEATURES))
    for row in profiles.itertuples():
        axes[1].plot(x, [getattr(row, f"z_{f}") for f in FEATURES], "o-", lw=2.5, color=palette[row.cluster_id], label=row.cluster_name)
    candidates = anomalies[anomalies.is_adverse_weather & anomalies.residual_z.gt(0)]
    highlight = candidates.iloc[0] if not candidates.empty else anomalies.iloc[0]
    station = assignments[assignments.canonical_station_id.eq(highlight.canonical_station_id)].iloc[0]
    values = np.array([station[f"z_{f}"] for f in FEATURES])
    axes[1].plot(x, values, "k--*", markersize=10, lw=2, label="Highlighted anomalous station")
    axes[1].annotate(f"{highlight.canonical_station_id.replace('CAN_', '')} | {highlight.trip_date:%Y-%m-%d}\n{highlight.weather_condition}, Z={highlight.residual_z:.2f}",
        xy=(len(FEATURES)-1, values[-1]), xytext=(-165, -48), textcoords="offset points", fontsize=9,
        bbox={"boxstyle": "round,pad=.35", "facecolor": "white", "alpha": .9}, arrowprops={"arrowstyle": "->", "color": "black"})
    axes[1].axhline(0, color="#777777", lw=1); axes[1].set_xticks(x, FEATURE_LABELS)
    axes[1].set(title="B. Cluster profiles and an adverse-weather outlier", ylabel="Standardized cluster feature (Z-score)")
    axes[1].legend(frameon=False, fontsize=8, loc="upper left"); sns.despine(ax=axes[1])
    fig.suptitle("Chicago station patterns and weather sensitivity: functional clusters and extreme anomalies", fontsize=20, fontweight="bold")
    fig.text(.5, .006, f"Data Source: {SOURCE} | Version/Accessed Date: {accessed} | Point size = mean daily origin trips; Lakefront is approximate", ha="center", fontsize=9, color="#555555")
    fig.savefig(output, dpi=300, bbox_inches="tight"); plt.close(fig)


def json_safe(value):
    if isinstance(value, dict): return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [json_safe(v) for v in value]
    if isinstance(value, np.integer): return int(value)
    if isinstance(value, (np.floating, float)): return float(value) if np.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, datetime, date)): return value.isoformat()
    return value


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    warnings.filterwarnings("ignore", category=FutureWarning)
    args = args_parser(); TABLE_DIR.mkdir(parents=True, exist_ok=True); FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    daily, hourly, weather = load_inputs(args.daily_panel, args.hourly_panel)
    station_days = station_day_grid(daily, weather)
    features, feature_summary = make_station_features(daily, hourly, station_days, args.min_station_trips)
    assignments, profiles, trials, cluster_summary = fit_clusters(features, args.random_state, args.accessed_date)
    top, ranked, anomaly_summary = detect_anomalies(station_days, assignments, args.accessed_date)
    thermal, bends = temperature_response(daily, weather, args.thermal_bin_width_c, args.accessed_date)
    retention, retention_summary = segment_retention(daily, weather, args.accessed_date)
    paths = {
        "cluster_profiles": TABLE_DIR/"03_station_clusters_profile.csv", "top_anomalies": TABLE_DIR/"03_top_anomalies.csv",
        "station_assignments": TABLE_DIR/"03_station_cluster_assignments.csv", "model_selection": TABLE_DIR/"03_cluster_model_selection.csv",
        "temperature_response": TABLE_DIR/"03_temperature_response.csv", "segment_weather_retention": TABLE_DIR/"03_segment_weather_retention.csv",
        "figure_1": FIGURE_DIR/"fig1_weather_nonlinear_response.png", "figure_2": FIGURE_DIR/"fig2_station_clusters_and_anomalies.png",
    }
    for frame, name in [(profiles,"cluster_profiles"),(top,"top_anomalies"),(assignments,"station_assignments"),(trials,"model_selection"),(thermal,"temperature_response"),(retention,"segment_weather_retention")]:
        frame.to_csv(paths[name], index=False)
    plot_weather(thermal, bends, retention, args.accessed_date, paths["figure_1"])
    plot_clusters(assignments, profiles, ranked, args.accessed_date, paths["figure_2"])
    sufficient = len(weather) >= 60 and weather.TMAX_C.max()-weather.TMAX_C.min() >= 18
    adverse = retention[retention.weather_condition.ne("Clear baseline") & retention.retention_ratio.notna()].sort_values("retention_ratio")
    weakest = adverse.iloc[0]
    metrics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "accessed_date": args.accessed_date, "data_source": SOURCE,
        "input": {"daily_rows": len(daily), "hourly_rows": len(hourly), "dates": len(weather), "date_min": weather.trip_date.min(), "date_max": weather.trip_date.max(), "total_trips": int(daily.trip_count.sum())},
        "station_feature_engineering": feature_summary, "clustering": cluster_summary, "anomaly_detection": anomaly_summary,
        "temperature_response": {"bin_width_c": args.thermal_bin_width_c, "marginal_bends_c": bends, "sufficient_for_confirmatory_bend": sufficient, "sufficiency_rule": ">=60 dates and >=18 C span"},
        "weather_retention": retention_summary,
        "key_readings": {"lowest_adverse_weather_retention": {"segment": weakest.segment, "condition": weakest.weather_condition, "retention_ratio": weakest.retention_ratio, "event_days": weakest.event_days}, "top_anomaly": top.iloc[0][["canonical_station_id","trip_date","trip_count","expected_trip_count","residual_z","weather_condition"]].to_dict()},
        "limitations": ["Current sample is a short synthetic winter validation set; claims are exploratory.", "Weather conditions overlap; retention indices are descriptive, not causal.", "Lakefront distance uses an approximate polyline."],
        "outputs": {name: str(path.relative_to(ROOT)) for name, path in paths.items()},
    }
    with (TABLE_DIR/"03_clustering_anomalies_metrics.json").open("w", encoding="utf-8") as handle:
        json.dump(json_safe(metrics), handle, ensure_ascii=False, indent=2, allow_nan=False)

    print("\n=== 第三、四项核心分析：关键读数 ===")
    print(f"1. 活跃站点：{feature_summary['active_stations']}/{feature_summary['all_stations']}；聚类模型={cluster_summary['selected_model']}，k={cluster_summary['selected_k']}，Silhouette={cluster_summary['silhouette_score']}.")
    if cluster_summary["small_sample_fallback"]: print(f"   样本警告：当前仅 {feature_summary['active_stations']} 个站点，k=3..6 无合法 Silhouette 候选，已使用 k={cluster_summary['selected_k']} 演示回退。")
    for row in profiles.itertuples(): print(f"   簇 {row.cluster_id} | {row.cluster_name}: {row.station_count} 站，日均={row.mean_daily_trips:.2f}，雨天留存={row.rain_retention_ratio:.2f}，低温/大风留存={row.cold_wind_retention_ratio:.2f}.")
    print("2. 温度边际拐点（探索性）："+", ".join(f"{name}={value:.1f}°C" if value is not None else f"{name}=不可识别" for name,value in bends.items()))
    if not sufficient: print("   充分性警告：日期数或温度跨度未达确认门槛，拐点仅用于流程与版式验证。")
    print(f"3. 最低恶劣天气需求指数：{weakest.segment} × {weakest.weather_condition} = {weakest.retention_ratio:.2f} (n={int(weakest.event_days)} 天).")
    print("4. Top 5 站点×日期异常：")
    for row in top.head().itertuples(): print(f"   #{row.anomaly_rank} {row.trip_date} {row.canonical_station_id}: 观测={row.trip_count:.0f}, 期望={row.expected_trip_count:.2f}, Z={row.residual_z:+.2f}, {row.weather_condition}.")
    print(f"5. 核心图已生成：{paths['figure_1'].relative_to(ROOT)}；{paths['figure_2'].relative_to(ROOT)}")
    print("   注意：当前为 17 天合成冬季样本，所有实体解释均属探索性流程验证。")
    LOG.info("PASS | profiles=%s, anomalies=%s, figures=2", len(profiles), len(top))


if __name__ == "__main__":
    main()
