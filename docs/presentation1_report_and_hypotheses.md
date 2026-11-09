# 第一次汇报完整报告初稿：Chicago Divvy × NOAA 天气 EDA

> **证据等级声明**：本轮是合成冬季管道验证样本，覆盖 2024-01-01 至 2024-01-17，共 17 天、239 次清洗后行程、3 个活跃站。结果提供线索，不支持总体外推或因果断言。

## 一、数据源、合规声明与清洗对齐流程

| 数据资产 | 角色 | 覆盖范围 | 记录/日期数 |
| --- | --- | --- | --- |
| Divvy trips | 合成冬季验证样本 | 2024-01-01T06:00:00-06:00 — 2024-01-17T00:25:00-06:00 | 240 |
| NOAA USW00094846 | O'Hare 主站 | 2024-01-01 — 2024-01-17 | 17 |
| NOAA USW00014819 | Midway 备站 | 2024-01-01 — 2024-01-17 | 17 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

输入资产名含 synthetic_，不能称作完整真实年度样本。文档只读取聚合产物，不嵌入或再分发原始行程；.gitignore 已排除 data/raw/ 与 data/processed/。

### 清洗漏斗

| 阶段 | 进入 | 删除 | 占初始样本 | 剩余 |
| --- | --- | --- | --- | --- |
| input | 240 | 0 | 0.000% | 240 |
| coordinates_missing_or_outside_chicago | 240 | 1 | 0.417% | 239 |
| clean_output | 239 | 0 | 0.000% | 239 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

- 删除 1 行坐标缺失/越界记录，删除率 0.417%、保留率 99.583%；其他清洗阶段合计删除 0 行。
- 按 America/Chicago 解析，以 trip_date 对齐天气并保留 trip_hour；得到 125 行 station-day 和 239 行小时特征，天气未匹配 0 行。
- 4 个历史 ID 经 50 m Haversine 归并为 3 个 canonical stations；修复 1 个同址异 ID 漂移簇，涉及 2 个历史 ID。
- NOAA 使用 mm、m/s、°C。O'Hare PRCP 有 1 日由 Midway 回补，最终剩余缺失 0。
- 2020 隔离：covid_trip_rows=0，240 条均为 2023_present；没有错误混池，也不能做疫情前后比较。

## 二、探索性数据分析核心发现（四类任务）

| 任务 | 真实计算结果 | 实证发现 | 边界 |
| --- | --- | --- | --- |
| 统计与相关性 | All×TMAX Pearson r=0.583, raw p=0.0141, FDR p=0.2532；Spearman ρ=0.496, FDR p=0.3851 | 较高 TMAX 与系统需求呈正向样本关系 | 17 天；FDR<0.05 共 0 项 |
| 分群天气留存 | Heavy rain: Casual-Classic=0.476，Casual-Electric=0.961；event days=2 | 客群×车型可能存在天气韧性异质性 | 晴朗基准 1 天；可靠估计 0 个 |
| 站点聚类 | CBD: 2 站/167 trips；Lakefront: 1 站/72 trips | 功能与天气留存轮廓分化 | k=2 回退；silhouette=0.0914 |
| 异常检测 | 2024-01-13 Lakefront observed=0.0, expected=3.478, z=-4.406；同日 CBD observed=7.0, expected=2.468, z=1.934 | 同一雪/积雪日出现相反站点异常 | Ridge 残差不是因果效应 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

1. **温度线索未通过多重检验。** casual 4.9°C 与 member 2.8°C 只是探索性弯点；温度跨度 7.4°C 未达确认条件。
2. **天气响应可能因客群×车型而异。** 差异值得设置交互项，但天气重叠且晴朗基准过少，不能解释为电助力因果保护。
3. **空间分群有线索但不稳定。** 只有 3 个站，k=2 是演示性回退，类别名不是已知真值。
4. **同日相反异常最值得验证。** 雪/积雪下 Lakefront 降为 0.0，CBD 同日正异常，提示保留 station×weather 异质性。

## 三、研究假设与后续实验蓝图

| 假设 | IV | DV | 调节变量 | 当前状态 |
| --- | --- | --- | --- | --- |
| H1 车型调节 | AWND_mps、TMAX_C | station-hour demand/retention | electric_bike、>3 km、公交荒漠 | Strong wind Casual classic/electric=0.936/1.083；Member=1.217/1.041；3 个事件日；最远距 Loop 1.732 km |
| H2 潮汐失衡 | 午后/晚高峰小时 PRCP | 净流量不对称与次日空桩 | CBD 类型、会员占比、净流出强度 | 当前只有日 PRCP，无法识别午后或跨日链条 |
| H3 韧性重构 | 疫情阶段×寒冷/微雨 | 聚类转移与天气弹性 | 社区属性、远程办公/短途接驳 | covid_trip_rows=0，均为 2023_present |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

### H1：电助力缓冲中长途通勤的逆风与中低温冲击

控制站点、小时、星期、季节与趋势后，AWND 上升/中低温对需求的负效应在 electric_bike 中更弱，且在 >3 km、轨道交通可达性低的站点更强。补充多年 trip distance 与 CTA/GTFS，构造完整 station-hour 零值面板，估计 weather×electric×long_trip×transit_desert 交互并做安慰剂检验。当前强风方向不一致，H1 未验证。

### H2：午后降雨放大 CBD 潮汐失衡并传导至次日空桩

晚高峰集中降雨提高 Net Flow Asymmetry 绝对值，并提高次日早高峰无车/空桩风险；效应随早高峰净流出增强。接入 NOAA ISD 小时雨量和 GBFS status，以窄窗事件研究匹配同站点、星期、季节的无雨日并检验提前趋势。日 PRCP 不能替代午后突发雨。

### H3：后疫情休闲社区站转为混合接驳站且天气弹性下降

相较疫情前，转向 mixed connector 的站点对寒冷/微雨的需求弹性绝对值下降，在远程办公和短途接驳代理较高社区更强。扩展到多年度四阶段数据，保持 canonical_station_id 和固定聚类特征，构造年度转移矩阵与站点事件研究；2020 单列断点。当前 covid_trip_rows=0，H3 完全待验证。

## 四、AI 使用分析（Vibe Coding 复盘）

| 环节 | AI（Codex）辅助 | 研究者独立判断与主导修正 |
| --- | --- | --- |
| 数据管道 | CSV/GBFS、日志、聚合输出 | 授权边界、时段与字段语义 |
| 时空对齐 | Haversine 50 m、NOAA 单位与插补 | 识别 station_id 漂移并决定 canonical 规则 |
| 异常与断点 | 相关、聚类、Ridge 批量计算 | 隔离 2020；区分 >24h 忘锁车与行为异常 |
| 表达与推断 | 多面板排版、数值注入、脚注 | 从相关线索构建可证伪机制 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 五、结论与边界

本轮完成清洗与时空对齐、统计与相关性、站点聚类与异常检测、两张高影响可视化。最可信的产出不是“显著效应”，而是三条可证伪路线；后续必须用更长时间、更广空间和外部数据验证。

## 附录：读取产物审计

生成器读取 outputs/tables/ 下全部 pre-04 CSV、JSON 与 Markdown；下表不含原始行程。

| 输入产物 | 格式 | 规模 | 单位 |
| --- | --- | --- | --- |
| 00_asset_inventory.csv | CSV | 3 | data rows |
| 00_data_inventory_metrics.json | JSON | 6 | top-level keys |
| 00_data_inventory_report.md | Markdown | 127 | lines |
| 00_divvy_missingness.csv | CSV | 13 | data rows |
| 00_noaa_quality.csv | CSV | 12 | data rows |
| 00_station_id_coordinate_comparison.csv | CSV | 2 | data rows |
| 01_clean_and_merge_metrics.json | JSON | 10 | top-level keys |
| 01_cleaning_funnel.csv | CSV | 8 | data rows |
| 01_station_harmonization_map.csv | CSV | 4 | data rows |
| 01_weather_daily_standardized.csv | CSV | 17 | data rows |
| 02_adverse_weather_retention.csv | CSV | 16 | data rows |
| 02_group_summary_stats.csv | CSV | 16 | data rows |
| 02_statistical_analysis_metrics.json | JSON | 11 | top-level keys |
| 02_thermal_thresholds.csv | CSV | 8 | data rows |
| 02_weather_correlations.csv | CSV | 36 | data rows |
| 03_cluster_model_selection.csv | CSV | 2 | data rows |
| 03_clustering_anomalies_metrics.json | JSON | 12 | top-level keys |
| 03_segment_weather_retention.csv | CSV | 20 | data rows |
| 03_station_cluster_assignments.csv | CSV | 3 | data rows |
| 03_station_clusters_profile.csv | CSV | 2 | data rows |
| 03_temperature_response.csv | CSV | 10 | data rows |
| 03_top_anomalies.csv | CSV | 20 | data rows |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>
