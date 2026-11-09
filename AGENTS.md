# Project Context: Chicago Divvy Bike-Sharing & NOAA Weather EDA (Presentation 1)

## 1. Objective & Scope (First Presentation Only)
- We are conducting Phase 1 Exploratory Data Analysis (EDA) via Vibe Coding on Chicago Divvy station-level trip data merged with NOAA daily/hourly weather observations.
- Goal for Presentation 1: Complete 3+ rigorous analytical tasks (Data Cleaning & Spatio-Temporal Alignment, Statistical & Correlation Analysis, Station Clustering & Anomaly Detection, and High-Impact Visualization), formulate 2-3 testable research hypotheses based on empirical anomalies/patterns, and log AI vs. Human contributions.
- DO NOT implement heavy downstream causal/counterfactual validation experiments or complex predictive deployment pipelines yet—those belong to Presentations 2-4.

## 2. Data Sources & License Compliance
- Divvy Trips & Stations: Sourced from https://divvybikes.com/system-data (or mirror https://data.cityofchicago.org/Transportation/Divvy-Trips/fg6s-gzvg).
- NOAA Weather: Daily Summaries from https://www.ncei.noaa.gov/access/past-weather/ or https://www.ncei.noaa.gov/cdo-web/ for Chicago O'Hare (`USW00094846`, primary) and Chicago Midway (`USW00014819`, backup/imputation). Hourly ISD from https://www.ncei.noaa.gov/products/land-based-station/integrated-surface-database if hourly join is requested.
- Compliance Rule: Divvy data license strictly prohibits redistributing raw data. Never hardcode or commit raw data rows into git-tracked code/markdown. Always maintain `.gitignore` excluding `data/raw/` and `data/processed/`, and automatically add a provenance footer (`Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: YYYY-MM-DD`) to every generated plot and summary table.

## 3. Domain-Specific Data Engineering Guardrails
- Timezone & Join Key: Parse `started_at` and `ended_at` in `America/Chicago` local time. Create `trip_date = started_at.dt.floor('D')` (and `trip_hour = started_at.dt.floor('h')`) to join with NOAA `DATE` (`PRCP`, `SNWD`, `SNOW`, `AWND`, `TMAX`, `TMIN`).
- Station ID Instability: Divvy `start_station_id` / `end_station_id` changes across years due to station relocation, renaming, and legacy integer vs. modern string IDs. Always build a station harmonization table by rounding (`lat`, `lng`) to 4 decimal places (~11m) or clustering stations within a 50-meter Haversine radius into a unified `canonical_station_id`.
- 2020 COVID-19 Anomaly: If the loaded data spans 2020, never pool 2020 blindly with post-2021 data. Explicitly tag regimes (`2019_pre_covid`, `2020_covid_shock`, `2021_2022_recovery`, `2023_present`) or isolate 2020 as a structural break anomaly check.
- Unit Standardization: Ensure NOAA metrics are standardized to SI/metric units (`TMAX`/`TMIN` in Celsius, `PRCP`/`SNOW`/`SNWD` in mm, `AWND` in m/s) with clear unit labels in column names and plot axes.

## 4. Coding & Visualization Standards
- Modular Python 3.10+ (`pandas`, `numpy`, `scipy`, `scikit-learn`, `statsmodels`, `matplotlib`, `seaborn`).
- All scripts must be reproducible, log data shapes/drop rates at each step, and save structured summary JSON/CSV metrics to `outputs/tables/` so we can inspect exact numbers.
- Every plot must be publication-ready (300 DPI, clean despine, colorblind-safe palette, clear title/subtitle stating the key finding, and source/version footnote).
