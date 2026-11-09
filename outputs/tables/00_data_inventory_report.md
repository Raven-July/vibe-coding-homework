# 数据资产与质量初筛报告

- 生成时间（UTC）：`2026-11-09T09:04:42.893139+00:00`
- 数据访问日期：`2026-11-09`
- 数据版本日期：`2024-01-17`
- Divvy 时间按 `America/Chicago` 解析；仅输出聚合指标。

## 1. 数据资产清单

| asset                                             | type               |   rows |   size_bytes | modified_utc                     | note   |
|:--------------------------------------------------|:-------------------|-------:|-------------:|:---------------------------------|:-------|
| data/raw/divvy/synthetic_divvy_trips.csv          | divvy_trip         |    240 |        37462 | 2026-11-09T09:02:03.720111+00:00 |        |
| data/raw/divvy/synthetic_station_information.json | divvy_station_gbfs |      4 |          725 | 2026-11-09T09:02:03.723420+00:00 |        |
| data/raw/noaa/synthetic_noaa_daily_summaries.csv  | noaa_weather       |     34 |         3117 | 2026-11-09T09:02:03.730664+00:00 |        |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 2. Divvy 时间跨度与规模

|   total_rows | started_at_min_chicago    | started_at_max_chicago    |   invalid_started_at |
|-------------:|:--------------------------|:--------------------------|---------------------:|
|          240 | 2024-01-01T06:00:00-06:00 | 2024-01-17T00:25:00-06:00 |                    0 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

### 字段缺失率

| field              |   missing_count |   missing_pct |
|:-------------------|----------------:|--------------:|
| ride_id            |               0 |        0      |
| rideable_type      |               0 |        0      |
| started_at         |               0 |        0      |
| ended_at           |               0 |        0      |
| start_station_name |               0 |        0      |
| start_station_id   |               0 |        0      |
| end_station_name   |               0 |        0      |
| end_station_id     |               1 |        0.4167 |
| start_lat          |               0 |        0      |
| start_lng          |               0 |        0      |
| end_lat            |               1 |        0.4167 |
| end_lng            |               1 |        0.4167 |
| member_casual      |               0 |        0      |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

### `rideable_type` 分布

| rideable_type   |   count |   share_pct |
|:----------------|--------:|------------:|
| electric_bike   |     132 |          55 |
| classic_bike    |     108 |          45 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

### `member_casual` 分布

| member_casual   |   count |   share_pct |
|:----------------|--------:|------------:|
| member          |     179 |     74.5833 |
| casual          |      61 |     25.4167 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

### COVID-19 阶段

| regime       |   count |   share_pct |
|:-------------|--------:|------------:|
| 2023_present |     240 |         100 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

2020 数据必须作为结构断点单列。

## 3. 站点 ID 稳定性

坐标按 4 位小数形成 `canonical_station_id` 候选。

| scope   |   station_id_unique |   coordinate_4dp_unique |   id_minus_coordinate |
|:--------|--------------------:|------------------------:|----------------------:|
| start   |                   4 |                       3 |                     1 |
| end     |                   4 |                       3 |                     1 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

### GBFS/站点资产

| asset                                             |   rows |   station_id_unique |   coordinate_4dp_unique |
|:--------------------------------------------------|-------:|--------------------:|------------------------:|
| data/raw/divvy/synthetic_station_information.json |      4 |                   4 |                       3 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 4. NOAA 主/备站量纲检查

合理性检查不替代 NCEI `units=metric` 元数据核验。

| station     |   rows | date_min   | date_max   |   tmax_lt_tmin_rows |
|:------------|-------:|:-----------|:-----------|--------------------:|
| USW00094846 |     17 | 2024-01-01 | 2024-01-17 |                   0 |
| USW00014819 |     17 | 2024-01-01 | 2024-01-17 |                   0 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

| station     | role             | metric   | expected_unit   |   rows |   missing_count |   missing_pct |   observed_min |   observed_max | unit_plausibility   |
|:------------|:-----------------|:---------|:----------------|-------:|----------------:|--------------:|---------------:|---------------:|:--------------------|
| USW00094846 | O'Hare (primary) | PRCP     | mm              |     17 |               1 |        5.8824 |            0   |           12.5 | PASS: SI-plausible  |
| USW00094846 | O'Hare (primary) | SNWD     | mm              |     17 |               0 |        0      |            0   |           22   | PASS: SI-plausible  |
| USW00094846 | O'Hare (primary) | SNOW     | mm              |     17 |               0 |        0      |            0   |            5.2 | PASS: SI-plausible  |
| USW00094846 | O'Hare (primary) | AWND     | m/s             |     17 |               0 |        0      |            2.1 |            8.5 | PASS: SI-plausible  |
| USW00094846 | O'Hare (primary) | TMAX     | °C              |     17 |               0 |        0      |            1.6 |            9   | PASS: SI-plausible  |
| USW00094846 | O'Hare (primary) | TMIN     | °C              |     17 |               0 |        0      |           -6.4 |            1   | PASS: SI-plausible  |
| USW00014819 | Midway (backup)  | PRCP     | mm              |     17 |               0 |        0      |            0   |            3.8 | PASS: SI-plausible  |
| USW00014819 | Midway (backup)  | SNWD     | mm              |     17 |               0 |        0      |            0   |           22   | PASS: SI-plausible  |
| USW00014819 | Midway (backup)  | SNOW     | mm              |     17 |               0 |        0      |            0   |            3.4 | PASS: SI-plausible  |
| USW00014819 | Midway (backup)  | AWND     | m/s             |     17 |               1 |        5.8824 |            3.4 |            8.1 | PASS: SI-plausible  |
| USW00014819 | Midway (backup)  | TMAX     | °C              |     17 |               0 |        0      |            2.3 |            9.7 | PASS: SI-plausible  |
| USW00014819 | Midway (backup)  | TMIN     | °C              |     17 |               0 |        0      |           -5.7 |            1.7 | PASS: SI-plausible  |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 5. 下一步

1. 修复 FAIL 后再合并。
2. 用 4 位坐标或 50 米 Haversine 聚类 harmonize 站点。
3. 记录清洗 shape/drop rate。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>