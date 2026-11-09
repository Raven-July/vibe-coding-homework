#!/usr/bin/env python3
"""Descriptive, weather-correlation, and nonlinear sensitivity analysis.

The input is the aggregate station-day panel produced by
``01_clean_and_merge.py``. No raw Divvy records are read or redistributed.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import warnings
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import ConstantInputWarning, pearsonr, spearmanr
from statsmodels.stats.multitest import multipletests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PANEL = ROOT / "data/processed/station_daily_weather_panel.parquet"
TABLE_DIR = ROOT / "outputs/tables"
SOURCE = "Divvy System Data & NOAA NCEI"
WEATHER_COLUMNS = {
    "TMAX": ("TMAX_C", "°C"),
    "TMIN": ("TMIN_C", "°C"),
    "PRCP": ("PRCP_mm", "mm"),
    "SNOW": ("SNOW_mm", "mm"),
    "SNWD": ("SNWD_mm", "mm"),
    "AWND": ("AWND_mps", "m/s"),
}
REQUIRED_COLUMNS = {
    "canonical_station_id", "trip_date", "member_casual", "rideable_type", "trip_count",
    "duration_median_min", "covid_period_flag", *(column for column, _ in WEATHER_COLUMNS.values()),
}
LOG = logging.getLogger("statistical_analysis")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--accessed-date", default=date.today().isoformat())
    parser.add_argument("--thermal-bin-width-c", type=float, default=3.0)
    parser.add_argument("--minimum-bin-days", type=int, default=3)
    args = parser.parse_args()
    try:
        date.fromisoformat(args.accessed_date)
    except ValueError:
        parser.error("--accessed-date must be YYYY-MM-DD")
    if args.thermal_bin_width_c <= 0:
        parser.error("--thermal-bin-width-c must be positive")
    if args.minimum_bin_days < 1:
        parser.error("--minimum-bin-days must be positive")
    return args


def season_from_month(month: pd.Series) -> pd.Series:
    return pd.Series(
        np.select(
            [month.isin([12, 1, 2]), month.isin([3, 4, 5]), month.isin([6, 7, 8])],
            ["Winter", "Spring", "Summer"],
            default="Autumn",
        ),
        index=month.index,
        dtype="string",
    )


def enrich_dates(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["trip_date"] = pd.to_datetime(result["trip_date"], errors="coerce").dt.normalize()
    result["day_type"] = np.where(result["trip_date"].dt.dayofweek.ge(5), "Weekend", "Weekday")
    result["day_of_week"] = result["trip_date"].dt.day_name()
    result["day_of_week_num"] = result["trip_date"].dt.dayofweek
    result["season"] = season_from_month(result["trip_date"].dt.month)
    result["month"] = result["trip_date"].dt.to_period("M").astype(str)
    result["covid_period"] = np.where(result["trip_date"].dt.year.eq(2020), "2020_covid", "non_2020")
    return result


def load_and_validate_panel(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    if not path.exists():
        raise FileNotFoundError(f"Input panel does not exist: {path}")
    panel = pd.read_parquet(path)
    missing = sorted(REQUIRED_COLUMNS - set(panel.columns))
    if missing:
        raise ValueError(f"Panel is missing required columns: {', '.join(missing)}")
    panel = enrich_dates(panel)
    if panel["trip_date"].isna().any():
        raise ValueError(f"Panel contains {int(panel['trip_date'].isna().sum())} invalid trip dates")
    for column in ["trip_count", "duration_median_min", *(item[0] for item in WEATHER_COLUMNS.values())]:
        panel[column] = pd.to_numeric(panel[column], errors="coerce")
    if panel["trip_count"].isna().any() or panel["trip_count"].lt(0).any():
        raise ValueError("trip_count must be complete and non-negative")
    if panel["duration_median_min"].isna().any() or panel["duration_median_min"].lt(0).any():
        raise ValueError("duration_median_min must be complete and non-negative")

    key = ["canonical_station_id", "trip_date", "member_casual", "rideable_type"]
    duplicate_keys = int(panel.duplicated(key).sum())
    if duplicate_keys:
        raise ValueError(f"Panel contains {duplicate_keys} duplicate composite keys")

    weather_columns = [column for column, _ in WEATHER_COLUMNS.values()]
    weather_variants = panel.groupby("trip_date")[weather_columns].nunique(dropna=False)
    inconsistent = int(weather_variants.gt(1).any(axis=1).sum())
    if inconsistent:
        raise ValueError(f"Weather values are inconsistent within {inconsistent} trip dates")
    weather = panel[["trip_date", *weather_columns]].drop_duplicates("trip_date").sort_values("trip_date")
    if weather[weather_columns].isna().any().any():
        counts = weather[weather_columns].isna().sum().to_dict()
        raise ValueError(f"Weather completeness gate failed: {counts}")

    full_dates = pd.date_range(weather["trip_date"].min(), weather["trip_date"].max(), freq="D")
    observed_dates = pd.DatetimeIndex(weather["trip_date"])
    missing_calendar_dates = full_dates.difference(observed_dates).strftime("%Y-%m-%d").tolist()
    if missing_calendar_dates:
        raise ValueError(
            "The station-day panel cannot distinguish zero-trip days from absent dates; "
            f"missing calendar dates: {missing_calendar_dates[:10]}"
        )
    integrity = {
        "status": "PASS",
        "panel_rows": int(len(panel)),
        "trip_count_sum": int(panel["trip_count"].sum()),
        "date_min": weather["trip_date"].min().date().isoformat(),
        "date_max": weather["trip_date"].max().date().isoformat(),
        "calendar_days": int(len(weather)),
        "duplicate_composite_keys": duplicate_keys,
        "inconsistent_weather_dates": inconsistent,
        "missing_calendar_dates": missing_calendar_dates,
        "weather_missing_cells": int(weather[weather_columns].isna().sum().sum()),
    }
    return panel, weather, integrity


def weighted_quantile(values: pd.Series, weights: pd.Series, probability: float) -> float:
    valid = values.notna() & weights.notna() & weights.gt(0)
    if not valid.any():
        return np.nan
    value_array = values.loc[valid].to_numpy(float)
    weight_array = weights.loc[valid].to_numpy(float)
    order = np.argsort(value_array)
    value_array = value_array[order]
    weight_array = weight_array[order]
    midpoint_cdf = (np.cumsum(weight_array) - 0.5 * weight_array) / weight_array.sum()
    return float(np.interp(probability, midpoint_cdf, value_array))


def add_provenance(frame: pd.DataFrame, accessed_date: str) -> pd.DataFrame:
    result = frame.copy()
    result["data_source"] = SOURCE
    result["version_accessed_date"] = accessed_date
    return result


def build_date_segment_grid(
    panel: pd.DataFrame, date_frame: pd.DataFrame, segment_columns: list[str]
) -> pd.DataFrame:
    segments = panel[segment_columns].drop_duplicates().sort_values(segment_columns)
    dates = date_frame[["trip_date"]].drop_duplicates().sort_values("trip_date")
    dates["_key"] = 1
    segments = segments.copy()
    segments["_key"] = 1
    grid = dates.merge(segments, on="_key").drop(columns="_key")
    daily = panel.groupby(["trip_date", *segment_columns], as_index=False)["trip_count"].sum()
    grid = grid.merge(daily, on=["trip_date", *segment_columns], how="left", validate="one_to_one")
    grid["trip_count"] = grid["trip_count"].fillna(0.0)
    return grid


def duration_summary(group: pd.DataFrame) -> pd.Series:
    q25 = weighted_quantile(group["duration_median_min"], group["trip_count"], 0.25)
    median = weighted_quantile(group["duration_median_min"], group["trip_count"], 0.50)
    q75 = weighted_quantile(group["duration_median_min"], group["trip_count"], 0.75)
    return pd.Series({
        "trip_duration_q25_min": q25,
        "trip_duration_median_min": median,
        "trip_duration_q75_min": q75,
        "trip_duration_iqr_min": q75 - q25,
    })


def group_summary_statistics(
    panel: pd.DataFrame, weather: pd.DataFrame, accessed_date: str
) -> pd.DataFrame:
    date_attributes = enrich_dates(weather)[
        ["trip_date", "day_type", "season", "covid_period"]
    ].drop_duplicates("trip_date")
    enriched_panel = panel.drop(columns=["day_type", "season", "covid_period"], errors="ignore").merge(
        date_attributes, on="trip_date", how="left", validate="many_to_one"
    )
    strata = ["day_type", "season", "covid_period"]
    specifications = [
        ("customer_type", ["member_casual"]),
        ("bike_type", ["rideable_type"]),
        ("customer_x_bike", ["member_casual", "rideable_type"]),
    ]
    outputs: list[pd.DataFrame] = []
    for scope, segment_columns in specifications:
        grid = build_date_segment_grid(panel, date_attributes, segment_columns)
        grid = grid.merge(date_attributes, on="trip_date", how="left", validate="many_to_one")
        group_columns = [*segment_columns, *strata]
        volume = (
            grid.groupby(group_columns, dropna=False, as_index=False)
            .agg(
                calendar_days=("trip_date", "size"),
                active_days=("trip_count", lambda x: int(x.gt(0).sum())),
                total_trips=("trip_count", "sum"),
                mean_daily_trips=("trip_count", "mean"),
                daily_trip_count_sd=("trip_count", "std"),
            )
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            duration = (
                enriched_panel.groupby(group_columns, dropna=False)
                .apply(duration_summary, include_groups=False)
                .reset_index()
            )
        summary = volume.merge(duration, on=group_columns, how="left", validate="one_to_one")
        summary["analysis_scope"] = scope
        if "member_casual" not in summary:
            summary["member_casual"] = "ALL"
        if "rideable_type" not in summary:
            summary["rideable_type"] = "ALL"
        outputs.append(summary)
    result = pd.concat(outputs, ignore_index=True)
    result["duration_quantile_method"] = "trip-weighted station-day cell medians (approximation)"
    columns = [
        "analysis_scope", "member_casual", "rideable_type", "day_type", "season", "covid_period",
        "calendar_days", "active_days", "total_trips", "mean_daily_trips", "daily_trip_count_sd",
        "trip_duration_q25_min", "trip_duration_median_min", "trip_duration_q75_min",
        "trip_duration_iqr_min", "duration_quantile_method",
    ]
    return add_provenance(result[columns].sort_values(columns[:6]).reset_index(drop=True), accessed_date)


def complete_daily_counts(panel: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    system = panel.groupby("trip_date", as_index=False)["trip_count"].sum()
    system["scope"] = "All"
    by_customer = build_date_segment_grid(panel, weather, ["member_casual"]).rename(
        columns={"member_casual": "scope"}
    )
    by_customer["scope"] = by_customer["scope"].str.title()
    counts = pd.concat(
        [system[["trip_date", "scope", "trip_count"]], by_customer[["trip_date", "scope", "trip_count"]]],
        ignore_index=True,
    )
    return counts.merge(weather, on="trip_date", how="left", validate="many_to_one")


def safe_correlation(x: pd.Series, y: pd.Series, method: str) -> tuple[float, float, int]:
    valid = x.notna() & y.notna()
    x_valid = x.loc[valid].astype(float)
    y_valid = y.loc[valid].astype(float)
    n = len(x_valid)
    if n < 3 or x_valid.nunique() < 2 or y_valid.nunique() < 2:
        return np.nan, np.nan, n
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConstantInputWarning)
        result = pearsonr(x_valid, y_valid) if method == "Pearson" else spearmanr(x_valid, y_valid)
    return float(result.statistic), float(result.pvalue), n


def adjust_pvalues(frame: pd.DataFrame, p_column: str, output_column: str) -> pd.DataFrame:
    result = frame.copy()
    result[output_column] = np.nan
    valid = result[p_column].notna()
    if valid.any():
        result.loc[valid, output_column] = multipletests(
            result.loc[valid, p_column].to_numpy(float), method="fdr_bh"
        )[1]
    return result


def weather_correlations(
    panel: pd.DataFrame, weather: pd.DataFrame, accessed_date: str
) -> pd.DataFrame:
    daily = complete_daily_counts(panel, weather)
    rows: list[dict[str, object]] = []
    for scope, group in daily.groupby("scope", sort=False):
        for metric, (column, unit) in WEATHER_COLUMNS.items():
            for method in ("Pearson", "Spearman"):
                coefficient, p_value, n = safe_correlation(group["trip_count"], group[column], method)
                rows.append({
                    "scope": scope,
                    "weather_metric": metric,
                    "weather_column": column,
                    "unit": unit,
                    "method": method,
                    "coefficient": coefficient,
                    "p_value_raw": p_value,
                    "n_days": n,
                })
    result = adjust_pvalues(pd.DataFrame(rows), "p_value_raw", "p_value_fdr_bh")
    result["significant_fdr_0_05"] = result["p_value_fdr_bh"].lt(0.05).fillna(False)
    return add_provenance(result, accessed_date)


def thermal_threshold_analysis(
    panel: pd.DataFrame,
    weather: pd.DataFrame,
    bin_width: float,
    minimum_bin_days: int,
    accessed_date: str,
) -> tuple[pd.DataFrame, dict[str, object]]:
    daily = build_date_segment_grid(panel, weather, ["member_casual"])
    daily = daily.merge(weather[["trip_date", "TMAX_C"]], on="trip_date", how="left", validate="many_to_one")
    low = math.floor(daily["TMAX_C"].min() / bin_width) * bin_width
    high = math.ceil(daily["TMAX_C"].max() / bin_width) * bin_width
    if high <= daily["TMAX_C"].max():
        high += bin_width
    edges = np.arange(low, high + bin_width * 0.5, bin_width)
    daily["temperature_bin"] = pd.cut(daily["TMAX_C"], bins=edges, right=False, include_lowest=True)
    result = (
        daily.groupby(["member_casual", "temperature_bin"], observed=True, as_index=False)
        .agg(
            bin_days=("trip_date", "nunique"),
            tmax_mean_c=("TMAX_C", "mean"),
            tmax_min_c=("TMAX_C", "min"),
            tmax_max_c=("TMAX_C", "max"),
            mean_daily_trips=("trip_count", "mean"),
            daily_trips_sd=("trip_count", "std"),
        )
    )
    result["temperature_bin_left_c"] = result["temperature_bin"].map(lambda interval: float(interval.left))
    result["temperature_bin_right_c"] = result["temperature_bin"].map(lambda interval: float(interval.right))
    result = result.sort_values(["member_casual", "temperature_bin_left_c"]).reset_index(drop=True)
    result["demand_change_pct_vs_previous_bin"] = (
        result.groupby("member_casual")["mean_daily_trips"].pct_change(fill_method=None) * 100.0
    )
    result["growth_acceleration_pct_points"] = result.groupby("member_casual")[
        "demand_change_pct_vs_previous_bin"
    ].diff()
    result["threshold_role"] = ""

    date_count = int(daily["trip_date"].nunique())
    temperature_span = float(daily["TMAX_C"].max() - daily["TMAX_C"].min())
    summaries: dict[str, object] = {
        "bin_width_c": bin_width,
        "minimum_bin_days": minimum_bin_days,
        "date_count": date_count,
        "temperature_min_c": float(daily["TMAX_C"].min()),
        "temperature_max_c": float(daily["TMAX_C"].max()),
        "temperature_span_c": temperature_span,
        "detection_requirements": "at least 60 days, 18°C span, and 6 supported bins; heat threshold must start at >=27°C",
        "by_customer_type": {},
    }
    for customer, indexes in result.groupby("member_casual").groups.items():
        customer_rows = result.loc[indexes].sort_values("temperature_bin_left_c")
        eligible = customer_rows.loc[customer_rows["bin_days"].ge(minimum_bin_days)]
        sufficient = date_count >= 60 and temperature_span >= 18.0 and len(eligible) >= 6
        startup: float | None = None
        heat: float | None = None
        if sufficient:
            acceleration = eligible.loc[eligible["growth_acceleration_pct_points"].gt(0)]
            if not acceleration.empty:
                startup_index = acceleration["growth_acceleration_pct_points"].idxmax()
                startup = float(result.at[startup_index, "temperature_bin_left_c"])
                result.at[startup_index, "threshold_role"] = "startup_acceleration"
            heat_candidates = eligible.loc[
                eligible["temperature_bin_left_c"].ge(27.0)
                & eligible["demand_change_pct_vs_previous_bin"].lt(0)
            ]
            if not heat_candidates.empty:
                heat_index = heat_candidates.index[0]
                heat = float(result.at[heat_index, "temperature_bin_left_c"])
                result.at[heat_index, "threshold_role"] = (
                    result.at[heat_index, "threshold_role"] + "|heat_suppression"
                ).strip("|")
        summaries["by_customer_type"][str(customer)] = {
            "supported_bins": int(len(eligible)),
            "data_sufficient_for_threshold_detection": bool(sufficient),
            "startup_threshold_c": startup,
            "heat_suppression_threshold_c": heat,
        }
    result["temperature_bin"] = result["temperature_bin"].astype(str)
    result["minimum_support_met"] = result["bin_days"].ge(minimum_bin_days)
    return add_provenance(result, accessed_date), summaries


def independent_design_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    candidates = pd.concat(
        [
            pd.Series(1.0, index=frame.index, name="const"),
            frame["event"].astype(float),
            pd.get_dummies(frame["day_of_week_num"], prefix="dow", drop_first=True, dtype=float),
            pd.get_dummies(frame["season"], prefix="season", drop_first=True, dtype=float),
            pd.get_dummies(frame["month"], prefix="month", drop_first=True, dtype=float),
        ],
        axis=1,
    )
    selected: list[str] = []
    current_rank = 0
    for column in candidates.columns:
        trial = candidates[selected + [column]].to_numpy(float)
        rank = int(np.linalg.matrix_rank(trial))
        if rank > current_rank:
            selected.append(column)
            current_rank = rank
    return candidates[selected]


def adjusted_poisson_retention(frame: pd.DataFrame) -> tuple[float, float, str, float]:
    if frame["event"].nunique() < 2:
        return np.nan, np.nan, "not_identifiable", np.nan
    design = independent_design_matrix(frame)
    if "event" not in design.columns or len(frame) <= design.shape[1]:
        return np.nan, np.nan, "insufficient_residual_df", float(len(frame) - design.shape[1])
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = sm.GLM(frame["trip_count"].astype(float), design, family=sm.families.Poisson()).fit(
                maxiter=200, disp=0
            )
        coefficient = float(fit.params["event"])
        p_value = float(fit.pvalues["event"])
        retention = float(np.exp(np.clip(coefficient, -20, 20)))
        status = "ok" if bool(fit.converged) else "not_converged"
        return retention, p_value, status, float(fit.df_resid)
    except Exception as error:  # statsmodels raises several data-dependent exception types
        return np.nan, np.nan, f"failed:{type(error).__name__}", np.nan


def raw_same_month_retention(frame: pd.DataFrame) -> tuple[float, int]:
    clear = frame.loc[frame["clear_day"]].groupby("month")["trip_count"].mean()
    event = frame.loc[frame["event"] & frame["month"].isin(clear.index)].copy()
    if event.empty:
        return np.nan, 0
    expected = event["month"].map(clear).sum()
    ratio = float(event["trip_count"].sum() / expected) if expected > 0 else np.nan
    return ratio, int(event["trip_date"].nunique())


def adverse_weather_retention(
    panel: pd.DataFrame, weather: pd.DataFrame, accessed_date: str
) -> tuple[pd.DataFrame, dict[str, object]]:
    weather_features = enrich_dates(weather)
    wind_p90 = float(weather_features["AWND_mps"].quantile(0.90))
    weather_features["light_rain"] = weather_features["PRCP_mm"].gt(0) & weather_features["PRCP_mm"].lt(5)
    weather_features["heavy_rain"] = weather_features["PRCP_mm"].ge(5)
    weather_features["snow_or_ice"] = weather_features["SNOW_mm"].gt(0) | weather_features["SNWD_mm"].gt(0)
    weather_features["strong_wind"] = weather_features["AWND_mps"].gt(wind_p90)
    weather_features["clear_day"] = (
        weather_features["PRCP_mm"].eq(0)
        & weather_features["SNOW_mm"].eq(0)
        & weather_features["SNWD_mm"].eq(0)
        & weather_features["AWND_mps"].le(wind_p90)
    )
    conditions = {
        "light_rain_0_to_5mm": "light_rain",
        "heavy_rain_ge_5mm": "heavy_rain",
        "snow_or_snow_depth": "snow_or_ice",
        "strong_wind_gt_p90": "strong_wind",
    }
    specifications = [
        ("customer_type", "member_casual", "member"),
        ("bike_type", "rideable_type", "classic_bike"),
    ]
    rows: list[dict[str, object]] = []
    for dimension, segment_column, reference in specifications:
        counts = build_date_segment_grid(panel, weather_features, [segment_column])
        counts = counts.merge(weather_features, on="trip_date", how="left", validate="many_to_one")
        for segment, segment_frame in counts.groupby(segment_column):
            for condition_name, condition_column in conditions.items():
                comparison = segment_frame.loc[
                    segment_frame[condition_column] | segment_frame["clear_day"]
                ].copy()
                comparison["event"] = comparison[condition_column].astype(bool)
                raw_ratio, matched_event_days = raw_same_month_retention(comparison)
                adjusted_ratio, p_value, status, residual_df = adjusted_poisson_retention(comparison)
                event_days = int(segment_frame[condition_column].sum())
                clear_days = int(segment_frame["clear_day"].sum())
                reliable = (
                    event_days >= 5
                    and clear_days >= 5
                    and matched_event_days >= 5
                    and status == "ok"
                    and residual_df >= 5
                    and np.isfinite(adjusted_ratio)
                )
                rows.append({
                    "segment_dimension": dimension,
                    "segment": segment,
                    "reference_segment": reference,
                    "weather_condition": condition_name,
                    "condition_definition": {
                        "light_rain_0_to_5mm": "0 < PRCP_mm < 5",
                        "heavy_rain_ge_5mm": "PRCP_mm >= 5",
                        "snow_or_snow_depth": "SNOW_mm > 0 or SNWD_mm > 0",
                        "strong_wind_gt_p90": f"AWND_mps > {wind_p90:.6g} (sample P90)",
                    }[condition_name],
                    "event_days": event_days,
                    "clear_baseline_days": clear_days,
                    "same_month_matched_event_days": matched_event_days,
                    "raw_same_month_retention_ratio": raw_ratio,
                    "fixed_effect_adjusted_retention_ratio": adjusted_ratio,
                    "fixed_effect_model": "Poisson log-link: event + weekday FE + season FE + month FE",
                    "event_p_value_raw": p_value,
                    "model_residual_df": residual_df,
                    "model_status": status,
                    "estimate_reliable": reliable,
                })
    result = pd.DataFrame(rows)
    result = adjust_pvalues(result, "event_p_value_raw", "event_p_value_fdr_bh")
    result["adjusted_retention_vs_reference"] = np.nan
    result["raw_retention_vs_reference"] = np.nan
    for (dimension, condition), indexes in result.groupby(["segment_dimension", "weather_condition"]).groups.items():
        group = result.loc[indexes]
        reference_name = group["reference_segment"].iloc[0]
        reference_rows = group.loc[group["segment"].eq(reference_name)]
        if reference_rows.empty:
            continue
        reference_adjusted = reference_rows["fixed_effect_adjusted_retention_ratio"].iloc[0]
        reference_raw = reference_rows["raw_same_month_retention_ratio"].iloc[0]
        if pd.notna(reference_adjusted) and reference_adjusted != 0:
            result.loc[indexes, "adjusted_retention_vs_reference"] = (
                result.loc[indexes, "fixed_effect_adjusted_retention_ratio"] / reference_adjusted
            )
        if pd.notna(reference_raw) and reference_raw != 0:
            result.loc[indexes, "raw_retention_vs_reference"] = (
                result.loc[indexes, "raw_same_month_retention_ratio"] / reference_raw
            )
    summary = {
        "wind_p90_mps": wind_p90,
        "clear_baseline_definition": "PRCP=0, SNOW=0, SNWD=0, and AWND<=sample P90",
        "clear_baseline_days": int(weather_features["clear_day"].sum()),
        "condition_day_counts": {name: int(weather_features[column].sum()) for name, column in conditions.items()},
        "reliable_estimates": int(result["estimate_reliable"].sum()),
        "reliability_rule": "event, clear, and matched event days >=5; residual df >=5; converged model",
    }
    return add_provenance(result, accessed_date), summary


def core_findings(
    panel: pd.DataFrame,
    correlations: pd.DataFrame,
    thermal_summary: dict[str, object],
    retention: pd.DataFrame,
    retention_summary: dict[str, object],
) -> list[str]:
    dates = int(panel["trip_date"].nunique())
    customer_daily = build_date_segment_grid(
        panel, panel[["trip_date"]].drop_duplicates(), ["member_casual"]
    ).groupby("member_casual")["trip_count"].mean()
    bike_daily = build_date_segment_grid(
        panel, panel[["trip_date"]].drop_duplicates(), ["rideable_type"]
    ).groupby("rideable_type")["trip_count"].mean()
    findings = [
        f"样本覆盖 {dates} 天、{int(panel['trip_count'].sum())} 次行程。",
        "日均客群单量：" + ", ".join(f"{name}={value:.2f}" for name, value in customer_daily.items()) + "。",
        "日均车型单量：" + ", ".join(f"{name}={value:.2f}" for name, value in bike_daily.items()) + "。",
    ]
    spearman = correlations.loc[
        correlations["method"].eq("Spearman") & correlations["coefficient"].notna()
    ].copy()
    if not spearman.empty:
        spearman["absolute_coefficient"] = spearman["coefficient"].abs()
        strongest = spearman.sort_values("absolute_coefficient", ascending=False).iloc[0]
        findings.append(
            "最强 Spearman 关系："
            f"{strongest['scope']} × {strongest['weather_metric']}，"
            f"rho={strongest['coefficient']:.3f}，FDR p={strongest['p_value_fdr_bh']:.4g}。"
        )
    significant = int(correlations["significant_fdr_0_05"].sum())
    findings.append(f"Pearson/Spearman 共发现 {significant} 个 FDR<0.05 的关系。")
    sufficient_types = [
        name for name, values in thermal_summary["by_customer_type"].items()
        if values["data_sufficient_for_threshold_detection"]
    ]
    if sufficient_types:
        for name in sufficient_types:
            values = thermal_summary["by_customer_type"][name]
            findings.append(
                f"{name} 温度启动/热抑制拐点：{values['startup_threshold_c']}°C / "
                f"{values['heat_suppression_threshold_c']}°C。"
            )
    else:
        findings.append(
            "温度拐点未作确认：日期、温度跨度或有效分箱未达到预设充分性门槛。"
        )
    reliable = retention.loc[retention["estimate_reliable"]]
    if reliable.empty:
        findings.append(
            "恶劣天气留存率均标为探索性："
            f"严格晴朗基准仅 {retention_summary['clear_baseline_days']} 天，无法可靠识别固定效应。"
        )
    else:
        strongest_retention = reliable.sort_values("fixed_effect_adjusted_retention_ratio").iloc[0]
        findings.append(
            "最低可靠留存率："
            f"{strongest_retention['segment']} / {strongest_retention['weather_condition']} = "
            f"{strongest_retention['fixed_effect_adjusted_retention_ratio']:.3f}。"
        )
    return findings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = parse_args()
    TABLE_DIR.mkdir(parents=True, exist_ok=True)
    panel, weather, integrity = load_and_validate_panel(args.panel)
    group_summary = group_summary_statistics(panel, weather, args.accessed_date)
    correlations = weather_correlations(panel, weather, args.accessed_date)
    thermal, thermal_summary = thermal_threshold_analysis(
        panel, weather, args.thermal_bin_width_c, args.minimum_bin_days, args.accessed_date
    )
    retention, retention_summary = adverse_weather_retention(panel, weather, args.accessed_date)
    findings = core_findings(panel, correlations, thermal_summary, retention, retention_summary)

    group_summary.to_csv(TABLE_DIR / "02_group_summary_stats.csv", index=False)
    correlations.to_csv(TABLE_DIR / "02_weather_correlations.csv", index=False)
    thermal.to_csv(TABLE_DIR / "02_thermal_thresholds.csv", index=False)
    retention.to_csv(TABLE_DIR / "02_adverse_weather_retention.csv", index=False)
    metrics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "accessed_date": args.accessed_date,
        "data_source": SOURCE,
        "input_panel": str(args.panel.relative_to(ROOT) if args.panel.is_relative_to(ROOT) else args.panel),
        "integrity": integrity,
        "multiple_testing": "Benjamini-Hochberg FDR across all finite correlation p-values",
        "duration_quantile_limitation": (
            "The station-day panel has no trip-level durations; duration quartiles are trip-count-weighted "
            "quantiles of station-day cell medians and are therefore approximations."
        ),
        "thermal_threshold_analysis": thermal_summary,
        "adverse_weather_retention": retention_summary,
        "core_findings": findings,
        "outputs": {
            "group_summary": "outputs/tables/02_group_summary_stats.csv",
            "weather_correlations": "outputs/tables/02_weather_correlations.csv",
            "thermal_thresholds": "outputs/tables/02_thermal_thresholds.csv",
            "adverse_weather_retention": "outputs/tables/02_adverse_weather_retention.csv",
        },
    }
    with (TABLE_DIR / "02_statistical_analysis_metrics.json").open("w", encoding="utf-8") as handle:
        json.dump(metrics, handle, ensure_ascii=False, indent=2, allow_nan=False)

    print("\n=== 核心统计发现 ===")
    for index, finding in enumerate(findings, start=1):
        print(f"{index}. {finding}")
    print("\n注意：当前输入为 17 天合成冬季样本；显著性与弹性估计仅用于流程验证。")
    LOG.info(
        "PASS | group rows=%s; correlations=%s; thermal bins=%s; retention rows=%s",
        len(group_summary), len(correlations), len(thermal), len(retention),
    )


if __name__ == "__main__":
    main()
