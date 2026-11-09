#!/usr/bin/env python3
"""Generate Presentation 1 Markdown deliverables from aggregate outputs only."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "Divvy System Data & NOAA NCEI"
TABLES, DOCS = ROOT / "outputs/tables", ROOT / "docs"
LOG = logging.getLogger("presentation1_deliverables")
REQUIRED = {
    "00_asset_inventory.csv", "00_data_inventory_metrics.json",
    "00_data_inventory_report.md", "00_divvy_missingness.csv",
    "00_noaa_quality.csv", "00_station_id_coordinate_comparison.csv",
    "01_clean_and_merge_metrics.json", "01_cleaning_funnel.csv",
    "01_station_harmonization_map.csv", "01_weather_daily_standardized.csv",
    "02_adverse_weather_retention.csv", "02_group_summary_stats.csv",
    "02_statistical_analysis_metrics.json", "02_thermal_thresholds.csv",
    "02_weather_correlations.csv", "03_cluster_model_selection.csv",
    "03_clustering_anomalies_metrics.json", "03_segment_weather_retention.csv",
    "03_station_cluster_assignments.csv", "03_station_clusters_profile.csv",
    "03_temperature_response.csv", "03_top_anomalies.csv",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables-dir", type=Path, default=TABLES)
    parser.add_argument("--docs-dir", type=Path, default=DOCS)
    return parser.parse_args()


def load_all(directory: Path) -> tuple[dict[str, Any], list[list[Any]]]:
    """Load every pre-04 CSV/JSON/Markdown artifact and audit its size."""
    paths = sorted(
        p for p in directory.iterdir()
        if p.suffix.lower() in {".csv", ".json", ".md"} and not p.name.startswith("04_")
    )
    missing = REQUIRED - {p.name for p in paths}
    if missing:
        raise FileNotFoundError(f"Missing aggregate artifacts: {sorted(missing)}")
    loaded, audit = {}, []
    for path in paths:
        if path.suffix == ".csv":
            value, kind, unit = pd.read_csv(path), "CSV", "data rows"
            size = len(value)
        elif path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            kind, size, unit = "JSON", len(value), "top-level keys"
        else:
            value = path.read_text(encoding="utf-8")
            kind, size, unit = "Markdown", len(value.splitlines()), "lines"
        loaded[path.name] = value
        audit.append([path.name, kind, size, unit])
    return loaded, audit


def validate(loaded: dict[str, Any]) -> str:
    sources, dates = set(), set()
    for name, value in loaded.items():
        if isinstance(value, pd.DataFrame):
            if not {"data_source", "version_accessed_date"}.issubset(value.columns):
                raise ValueError(f"Missing provenance columns: {name}")
            sources.update(value["data_source"].dropna().astype(str).unique())
            dates.update(value["version_accessed_date"].dropna().astype(str).unique())
        elif isinstance(value, dict):
            if "data_source" in value:
                sources.add(str(value["data_source"]))
            if "accessed_date" in value:
                dates.add(str(value["accessed_date"]))
    if sources != {SOURCE} or len(dates) != 1:
        raise ValueError(f"Inconsistent provenance: sources={sources}, dates={dates}")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    if any(rule not in gitignore for rule in ("/data/raw/**", "/data/processed/**")):
        raise ValueError(".gitignore must exclude data/raw and data/processed")
    return dates.pop()


def row(frame: pd.DataFrame, **filters: Any) -> pd.Series:
    result = frame
    for column, value in filters.items():
        result = result[result[column].eq(value)]
    if len(result) != 1:
        raise ValueError(f"Expected one row for {filters}; found {len(result)}")
    return result.iloc[0]


def n(value: Any) -> str:
    return f"{int(value):,}"


def d(value: Any, digits: int = 3) -> str:
    return f"{float(value):.{digits}f}"


def pct_value(value: Any, digits: int = 3) -> str:
    return f"{float(value):.{digits}f}%"


def pct_ratio(value: Any, digits: int = 1) -> str:
    return f"{100 * float(value):.{digits}f}%"


def footer(accessed: str) -> str:
    return f"<sub>Data Source: {SOURCE} | Version/Accessed Date: {accessed}</sub>"


def md_table(headers: list[str], rows: list[list[Any]], accessed: str) -> str:
    def esc(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br>")
    lines = [
        "| " + " | ".join(map(esc, headers)) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines += ["| " + " | ".join(map(esc, values)) + " |" for values in rows]
    return "\n".join(lines) + "\n\n" + footer(accessed)


def extract(a: dict[str, Any]) -> dict[str, Any]:
    inv, clean = a["00_data_inventory_metrics.json"], a["01_clean_and_merge_metrics.json"]
    stats = a["02_statistical_analysis_metrics.json"]
    clus = a["03_clustering_anomalies_metrics.json"]
    corr, ret = a["02_weather_correlations.csv"], a["03_segment_weather_retention.csv"]
    prof = a["03_station_clusters_profile.csv"]
    assn, anom = a["03_station_cluster_assignments.csv"], a["03_top_anomalies.csv"]
    top = anom.iloc[0]
    same_day = row(anom, canonical_station_id="CAN_5AD38AB76E", trip_date=str(top["trip_date"]))
    return {
        "inv": inv, "clean": clean, "stats": stats, "clus": clus,
        "pearson": row(corr, scope="All", weather_metric="TMAX", method="Pearson"),
        "spearman": row(corr, scope="All", weather_metric="TMAX", method="Spearman"),
        "fdr_hits": int(corr["significant_fdr_0_05"].astype(str).str.lower().eq("true").sum()),
        "cc_heavy": row(ret, segment="Casual-Classic", weather_condition="Heavy rain"),
        "ce_heavy": row(ret, segment="Casual-Electric", weather_condition="Heavy rain"),
        "cc_wind": row(ret, segment="Casual-Classic", weather_condition="Strong wind"),
        "ce_wind": row(ret, segment="Casual-Electric", weather_condition="Strong wind"),
        "mc_wind": row(ret, segment="Member-Classic", weather_condition="Strong wind"),
        "me_wind": row(ret, segment="Member-Electric", weather_condition="Strong wind"),
        "cbd": row(prof, cluster_name="CBD rigid commuter"),
        "leisure": row(prof, cluster_name="Lakefront leisure"),
        "top": top, "same_day": same_day,
        "max_loop_km": float(assn["distance_loop_km"].max()),
    }


def report(x: dict[str, Any], audit: list[list[Any]], accessed: str) -> str:
    inv, clean, stats, clus = x["inv"], x["clean"], x["stats"], x["clus"]
    divvy, ohare, midway = inv["divvy"], *inv["noaa"]
    funnel, harm = clean["trip_cleaning"], clean["station_harmonization"]
    weather, panels, integ = clean["weather_standardization"], clean["panels"], stats["integrity"]
    funnel_rows = {item["stage"]: item for item in funnel["funnel"]}
    non_coordinate_drops = sum(
        item["rows_dropped"] for item in funnel["funnel"]
        if item["stage"] != "coordinates_missing_or_outside_chicago"
    )
    sources = md_table(
        ["数据资产", "角色", "覆盖范围", "记录/日期数"],
        [
            ["Divvy trips", "合成冬季验证样本",
             f"{divvy['started_at_min_chicago']} — {divvy['started_at_max_chicago']}", n(divvy["rows"])],
            ["NOAA USW00094846", "O'Hare 主站",
             f"{ohare['date_min']} — {ohare['date_max']}", n(ohare["rows"])],
            ["NOAA USW00014819", "Midway 备站",
             f"{midway['date_min']} — {midway['date_max']}", n(midway["rows"])],
        ], accessed,
    )
    funnel_table = md_table(
        ["阶段", "进入", "删除", "占初始样本", "剩余"],
        [
            ["input", n(funnel_rows["input"]["rows_entering"]),
             n(funnel_rows["input"]["rows_dropped"]),
             pct_value(funnel_rows["input"]["drop_pct_of_initial"]),
             n(funnel_rows["input"]["rows_remaining"])],
            ["coordinates_missing_or_outside_chicago", n(funnel["input_rows"]),
             n(funnel["total_dropped"]), pct_value(funnel["total_drop_pct"]), n(funnel["clean_rows"])],
            ["clean_output", n(funnel_rows["clean_output"]["rows_entering"]),
             n(funnel_rows["clean_output"]["rows_dropped"]),
             pct_value(funnel_rows["clean_output"]["drop_pct_of_initial"]),
             n(funnel_rows["clean_output"]["rows_remaining"])],
        ], accessed,
    )
    findings = md_table(
        ["任务", "真实计算结果", "实证发现", "边界"],
        [
            ["统计与相关性",
             f"All×TMAX Pearson r={d(x['pearson']['coefficient'])}, raw p={d(x['pearson']['p_value_raw'],4)}, FDR p={d(x['pearson']['p_value_fdr_bh'],4)}；Spearman ρ={d(x['spearman']['coefficient'])}, FDR p={d(x['spearman']['p_value_fdr_bh'],4)}",
             "较高 TMAX 与系统需求呈正向样本关系",
             f"{n(integ['calendar_days'])} 天；FDR<0.05 共 {n(x['fdr_hits'])} 项"],
            ["分群天气留存",
             f"Heavy rain: Casual-Classic={d(x['cc_heavy']['retention_ratio'])}，Casual-Electric={d(x['ce_heavy']['retention_ratio'])}；event days={n(x['cc_heavy']['event_days'])}",
             "客群×车型可能存在天气韧性异质性",
             f"晴朗基准 {n(stats['adverse_weather_retention']['clear_baseline_days'])} 天；可靠估计 {n(stats['adverse_weather_retention']['reliable_estimates'])} 个"],
            ["站点聚类",
             f"CBD: {n(x['cbd']['station_count'])} 站/{n(x['cbd']['total_trips'])} trips；Lakefront: {n(x['leisure']['station_count'])} 站/{n(x['leisure']['total_trips'])} trips",
             "功能与天气留存轮廓分化",
             f"k={n(clus['clustering']['selected_k'])} 回退；silhouette={d(clus['clustering']['silhouette_score'],4)}"],
            ["异常检测",
             f"{x['top']['trip_date']} Lakefront observed={d(x['top']['trip_count'],1)}, expected={d(x['top']['expected_trip_count'])}, z={d(x['top']['residual_z'])}；同日 CBD observed={d(x['same_day']['trip_count'],1)}, expected={d(x['same_day']['expected_trip_count'])}, z={d(x['same_day']['residual_z'])}",
             "同一雪/积雪日出现相反站点异常", "Ridge 残差不是因果效应"],
        ], accessed,
    )
    hypotheses = md_table(
        ["假设", "IV", "DV", "调节变量", "当前状态"],
        [
            ["H1 车型调节", "AWND_mps、TMAX_C", "station-hour demand/retention",
             "electric_bike、>3 km、公交荒漠",
             f"Strong wind Casual classic/electric={d(x['cc_wind']['retention_ratio'])}/{d(x['ce_wind']['retention_ratio'])}；Member={d(x['mc_wind']['retention_ratio'])}/{d(x['me_wind']['retention_ratio'])}；{n(x['cc_wind']['event_days'])} 个事件日；最远距 Loop {d(x['max_loop_km'])} km"],
            ["H2 潮汐失衡", "午后/晚高峰小时 PRCP", "净流量不对称与次日空桩",
             "CBD 类型、会员占比、净流出强度", "当前只有日 PRCP，无法识别午后或跨日链条"],
            ["H3 韧性重构", "疫情阶段×寒冷/微雨", "聚类转移与天气弹性",
             "社区属性、远程办公/短途接驳",
             f"covid_trip_rows={n(panels['covid_trip_rows'])}，均为 2023_present"],
        ], accessed,
    )
    ai = md_table(
        ["环节", "AI（Codex）辅助", "研究者独立判断与主导修正"],
        [
            ["数据管道", "CSV/GBFS、日志、聚合输出", "授权边界、时段与字段语义"],
            ["时空对齐", f"Haversine {n(harm['clustering_radius_m'])} m、NOAA 单位与插补", "识别 station_id 漂移并决定 canonical 规则"],
            ["异常与断点", "相关、聚类、Ridge 批量计算", "隔离 2020；区分 >24h 忘锁车与行为异常"],
            ["表达与推断", "多面板排版、数值注入、脚注", "从相关线索构建可证伪机制"],
        ], accessed,
    )
    audit_table = md_table(["输入产物", "格式", "规模", "单位"], audit, accessed)
    return f"""# 第一次汇报完整报告初稿：Chicago Divvy × NOAA 天气 EDA

> **证据等级声明**：本轮是合成冬季管道验证样本，覆盖 {integ['date_min']} 至 {integ['date_max']}，共 {n(integ['calendar_days'])} 天、{n(integ['trip_count_sum'])} 次清洗后行程、{n(clus['station_feature_engineering']['active_stations'])} 个活跃站。结果提供线索，不支持总体外推或因果断言。

## 一、数据源、合规声明与清洗对齐流程

{sources}

输入资产名含 synthetic_，不能称作完整真实年度样本。文档只读取聚合产物，不嵌入或再分发原始行程；.gitignore 已排除 data/raw/ 与 data/processed/。

### 清洗漏斗

{funnel_table}

- 删除 {n(funnel['total_dropped'])} 行坐标缺失/越界记录，删除率 {pct_value(funnel['total_drop_pct'])}、保留率 {pct_value(100-funnel['total_drop_pct'])}；其他清洗阶段合计删除 {n(non_coordinate_drops)} 行。
- 按 America/Chicago 解析，以 trip_date 对齐天气并保留 trip_hour；得到 {n(panels['daily_panel_rows'])} 行 station-day 和 {n(panels['hourly_feature_rows'])} 行小时特征，天气未匹配 {n(panels['daily_panel_unmatched_weather_rows'])} 行。
- {n(harm['historical_station_ids'])} 个历史 ID 经 {n(harm['clustering_radius_m'])} m Haversine 归并为 {n(harm['canonical_stations'])} 个 canonical stations；修复 {n(harm['same_location_different_id_clusters'])} 个同址异 ID 漂移簇，涉及 {n(harm['historical_ids_in_same_location_different_id'])} 个历史 ID。
- NOAA 使用 mm、m/s、°C。O'Hare PRCP 有 {n(weather['imputed_from_midway_by_metric']['PRCP'])} 日由 Midway 回补，最终剩余缺失 {n(sum(weather['remaining_missing_by_metric_all_dates'].values()))}。
- 2020 隔离：covid_trip_rows={n(panels['covid_trip_rows'])}，{n(divvy['regime_counts']['2023_present'])} 条均为 2023_present；没有错误混池，也不能做疫情前后比较。

## 二、探索性数据分析核心发现（四类任务）

{findings}

1. **温度线索未通过多重检验。** casual {d(clus['temperature_response']['marginal_bends_c']['casual'],1)}°C 与 member {d(clus['temperature_response']['marginal_bends_c']['member'],1)}°C 只是探索性弯点；温度跨度 {d(stats['thermal_threshold_analysis']['temperature_span_c'],1)}°C 未达确认条件。
2. **天气响应可能因客群×车型而异。** 差异值得设置交互项，但天气重叠且晴朗基准过少，不能解释为电助力因果保护。
3. **空间分群有线索但不稳定。** 只有 {n(clus['station_feature_engineering']['active_stations'])} 个站，k={n(clus['clustering']['selected_k'])} 是演示性回退，类别名不是已知真值。
4. **同日相反异常最值得验证。** 雪/积雪下 Lakefront 降为 {d(x['top']['trip_count'],1)}，CBD 同日正异常，提示保留 station×weather 异质性。

## 三、研究假设与后续实验蓝图

{hypotheses}

### H1：电助力缓冲中长途通勤的逆风与中低温冲击

控制站点、小时、星期、季节与趋势后，AWND 上升/中低温对需求的负效应在 electric_bike 中更弱，且在 >3 km、轨道交通可达性低的站点更强。补充多年 trip distance 与 CTA/GTFS，构造完整 station-hour 零值面板，估计 weather×electric×long_trip×transit_desert 交互并做安慰剂检验。当前强风方向不一致，H1 未验证。

### H2：午后降雨放大 CBD 潮汐失衡并传导至次日空桩

晚高峰集中降雨提高 Net Flow Asymmetry 绝对值，并提高次日早高峰无车/空桩风险；效应随早高峰净流出增强。接入 NOAA ISD 小时雨量和 GBFS status，以窄窗事件研究匹配同站点、星期、季节的无雨日并检验提前趋势。日 PRCP 不能替代午后突发雨。

### H3：后疫情休闲社区站转为混合接驳站且天气弹性下降

相较疫情前，转向 mixed connector 的站点对寒冷/微雨的需求弹性绝对值下降，在远程办公和短途接驳代理较高社区更强。扩展到多年度四阶段数据，保持 canonical_station_id 和固定聚类特征，构造年度转移矩阵与站点事件研究；2020 单列断点。当前 covid_trip_rows={n(panels['covid_trip_rows'])}，H3 完全待验证。

## 四、AI 使用分析（Vibe Coding 复盘）

{ai}

## 五、结论与边界

本轮完成清洗与时空对齐、统计与相关性、站点聚类与异常检测、两张高影响可视化。最可信的产出不是“显著效应”，而是三条可证伪路线；后续必须用更长时间、更广空间和外部数据验证。

## 附录：读取产物审计

生成器读取 outputs/tables/ 下全部 pre-04 CSV、JSON 与 Markdown；下表不含原始行程。

{audit_table}

{footer(accessed)}
"""


def ppt(x: dict[str, Any], accessed: str) -> str:
    inv, clean, stats, clus = x["inv"], x["clean"], x["stats"], x["clus"]
    divvy, funnel = inv["divvy"], clean["trip_cleaning"]
    harm, panels, integ = clean["station_harmonization"], clean["panels"], stats["integrity"]
    overview = md_table(
        ["页", "标题", "Assertion", "Evidence"],
        [
            [1, "研究问题与证据等级", "先建可信管道，再提出假设",
             f"{n(integ['calendar_days'])} 天/{n(integ['trip_count_sum'])} trips/合成样本"],
            [2, "数据与清洗", "质量损失可审计", f"{n(funnel['input_rows'])}→{n(funnel['clean_rows'])}"],
            [3, "站点与气象对齐", "先修复 ID 和缺失",
             f"{n(harm['historical_station_ids'])} IDs→{n(harm['canonical_stations'])} canonical"],
            [4, "统计证据", "温度有线索但未通过 FDR",
             f"r={d(x['pearson']['coefficient'])}，FDR p={d(x['pearson']['p_value_fdr_bh'],4)}"],
            [5, "Fig 1", "天气响应可能受客群与车型调节",
             f"heavy rain {d(x['cc_heavy']['retention_ratio'])} vs {d(x['ce_heavy']['retention_ratio'])}"],
            [6, "Fig 2", "系统均值遮蔽相反站点异常",
             f"k={n(clus['clustering']['selected_k'])}，silhouette={d(clus['clustering']['silhouette_score'],4)}"],
            [7, "Vibe Coding", "AI 扩展计算，研究者负责边界", "责任矩阵+局限"],
            [8, "假设与下阶段", "补齐数据与识别，而非扩大结论", "H1/H2/H3"],
        ], accessed,
    )
    return f"""# 第一次课堂汇报：8 页 PPT 逐页讲稿与排版蓝图

> 每页采用 Assertion-Evidence；统一页脚为 Data Source: {SOURCE} | Version/Accessed Date: {accessed}。本轮为合成冬季验证样本。

## 总览

{overview}

## 第 1 页｜研究问题与证据等级

**Assertion**：先用可审计 EDA 找到“哪里不同”，再把异常转成可证伪机制。  
**排版**：左侧研究链条；右侧卡片：{n(integ['calendar_days'])} 天、{n(integ['trip_count_sum'])} trips、{n(clus['station_feature_engineering']['active_stations'])} stations；加 synthetic winter validation 标签。  
**讲稿**：本次完成四类任务，重点是天气响应差异和空间异质性，不是年度总体结论。

{footer(accessed)}

## 第 2 页｜数据源、许可与清洗漏斗

**Assertion**：质量损失小且可追踪，原始 Divvy 行程不进入交付文档。  
**排版**：Divvy、O'Hare USW00094846、Midway USW00014819 三卡片；漏斗 {n(funnel['input_rows'])}→{n(funnel['clean_rows'])}，删除 {n(funnel['total_dropped'])} 行，删除率 {pct_value(funnel['total_drop_pct'])}、保留率 {pct_value(100-funnel['total_drop_pct'])}。  
**讲稿**：时间按 Chicago 本地时区，天气按日连接；raw/processed 均被 gitignore 排除。

{footer(accessed)}

## 第 3 页｜站点 ID 修复与气象对齐

**Assertion**：不先修复 ID 漂移，跨期画像和异常会被人为拆散。  
**排版**：{n(harm['historical_station_ids'])} IDs 经 {n(harm['clustering_radius_m'])} m Haversine 归并为 {n(harm['canonical_stations'])} stations；突出 {n(harm['same_location_different_id_clusters'])} 个漂移簇、{n(harm['historical_ids_in_same_location_different_id'])} 个历史 ID。右侧写 PRCP 回补 {n(clean['weather_standardization']['imputed_from_midway_by_metric']['PRCP'])} 日，最终缺失 {n(sum(clean['weather_standardization']['remaining_missing_by_metric_all_dates'].values()))}。  
**讲稿**：covid_trip_rows={n(panels['covid_trip_rows'])}，{n(divvy['regime_counts']['2023_present'])} 条均为 2023_present；只能说明没有混池，不能做疫情前后比较。

{footer(accessed)}

## 第 4 页｜统计分析：线索不等于显著

**Assertion**：温度与需求正相关，但短样本和多重检验使其仅为线索。  
**排版**：Pearson r={d(x['pearson']['coefficient'])}、raw p={d(x['pearson']['p_value_raw'],4)}、FDR p={d(x['pearson']['p_value_fdr_bh'],4)}；Spearman ρ={d(x['spearman']['coefficient'])}、FDR p={d(x['spearman']['p_value_fdr_bh'],4)}。红框：FDR<0.05 共 {n(x['fdr_hits'])} 项，n={n(x['pearson']['n_days'])} days。  
**讲稿**：原始 p 值不能越过 FDR；温度跨度 {d(stats['thermal_threshold_analysis']['temperature_span_c'],1)}°C，不能确认阈值。

{footer(accessed)}

## 第 5 页｜Fig 1：天气响应由客群与车型共同塑造

**Assertion**：总体天气效应可能掩盖客群×车型韧性差异。  
**主图**：全宽放 ../outputs/figures/fig1_weather_nonlinear_response.png。  
**图 A 话术**：casual 弯点约 {d(clus['temperature_response']['marginal_bends_c']['casual'],1)}°C、member 约 {d(clus['temperature_response']['marginal_bends_c']['member'],1)}°C，均为 exploratory，置信区间宽且不满足确认规则。  
**图 B 话术**：heavy rain 下 Casual-Classic 留存 {pct_ratio(x['cc_heavy']['retention_ratio'])}、Casual-Electric {pct_ratio(x['ce_heavy']['retention_ratio'])}，但只有 {n(x['cc_heavy']['event_days'])} 个事件日；晴朗基准 {n(stats['adverse_weather_retention']['clear_baseline_days'])} 天。图用于提出调节假设，不用于证明。

{footer(accessed)}

## 第 6 页｜Fig 2：空间分群与同日相反异常

**Assertion**：同一天同类恶劣天气下，不同功能站可出现相反异常。  
**主图**：全宽放 ../outputs/figures/fig2_station_clusters_and_anomalies.png，并加小样本警示。  
**左图话术**：CBD 类 {n(x['cbd']['station_count'])} 站/{n(x['cbd']['total_trips'])} trips；Lakefront 类 {n(x['leisure']['station_count'])} 站/{n(x['leisure']['total_trips'])} trips。k={n(clus['clustering']['selected_k'])} 为回退，silhouette={d(clus['clustering']['silhouette_score'],4)}，不可外推。  
**右图话术**：{x['top']['trip_date']} Lakefront observed={d(x['top']['trip_count'],1)}、expected={d(x['top']['expected_trip_count'])}、z={d(x['top']['residual_z'])}；同日 CBD 为正异常。下一阶段应估计 station×weather 异质性。

{footer(accessed)}

## 第 7 页｜Vibe Coding：AI 扩展计算，研究者守住解释边界

**Assertion**：可信 Vibe Coding 是代码扩展性与人类判断责任的分工。  
**排版**：左侧 AI：CSV/GBFS、Haversine、NOAA 单位/插补、统计和多面板排版；右侧研究者：ID 业务漂移、2020 断点、机械/行为异常区分、机制假设。页底标签：短合成样本、{n(clus['station_feature_engineering']['active_stations'])} stations、条件重叠、非因果。  
**讲稿**：AI 提高实现与复现效率；合并规则、断点、研究价值和识别策略由研究者主导。

{footer(accessed)}

## 第 8 页｜研究假设与下阶段实验预告

**Assertion**：下一阶段补齐每条机制缺口，而非扩大当前结论。  
**排版**：三列卡片“假设—缺数据—识别方法—失败判据”。  
1. **H1 车型调节**：天气×electric×>3 km×transit desert；补多年距离与 CTA/GTFS；station-hour 固定效应计数模型。
2. **H2 潮汐失衡**：小时 PRCP→晚高峰 net flow→次日空桩；补 NOAA ISD 与 GBFS status；窄窗事件研究。
3. **H3 韧性重构**：疫情阶段×cluster transition×weather elasticity；补疫情前后多年样本；固定特征转移矩阵与站点事件研究。

**收束讲稿**：H1 缺长距离和公共交通可达性，H2 缺小时降雨与库存，H3 缺疫情前后年份；下一次汇报围绕这些缺口验证。

{footer(accessed)}
"""


def main() -> None:
    options = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    loaded, audit = load_all(options.tables_dir)
    accessed = validate(loaded)
    values = extract(loaded)
    options.docs_dir.mkdir(parents=True, exist_ok=True)
    report_path = options.docs_dir / "presentation1_report_and_hypotheses.md"
    ppt_path = options.docs_dir / "presentation1_ppt_outline.md"
    report_path.write_text(report(values, audit, accessed), encoding="utf-8")
    ppt_path.write_text(ppt(values, accessed), encoding="utf-8")
    manifest = {
        "data_source": SOURCE,
        "accessed_date": accessed,
        "generator": str(Path(__file__).relative_to(ROOT)),
        "input_artifacts": [item[0] for item in audit],
        "output_documents": [
            str(report_path.relative_to(ROOT)),
            str(ppt_path.relative_to(ROOT)),
        ],
        "empirical_number_policy": "All empirical numbers are read from outputs/tables at runtime.",
    }
    manifest_path = options.tables_dir / "04_presentation1_deliverables_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    LOG.info("Read %s aggregate artifacts", len(audit))
    LOG.info("Wrote %s", report_path)
    LOG.info("Wrote %s", ppt_path)
    LOG.info("Wrote %s", manifest_path)


if __name__ == "__main__":
    main()
