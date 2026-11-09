# 第一次课堂汇报：8 页 PPT 逐页讲稿与排版蓝图

> 每页采用 Assertion-Evidence；统一页脚为 Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09。本轮为合成冬季验证样本。

## 总览

| 页 | 标题 | Assertion | Evidence |
| --- | --- | --- | --- |
| 1 | 研究问题与证据等级 | 先建可信管道，再提出假设 | 17 天/239 trips/合成样本 |
| 2 | 数据与清洗 | 质量损失可审计 | 240→239 |
| 3 | 站点与气象对齐 | 先修复 ID 和缺失 | 4 IDs→3 canonical |
| 4 | 统计证据 | 温度有线索但未通过 FDR | r=0.583，FDR p=0.2532 |
| 5 | Fig 1 | 天气响应可能受客群与车型调节 | heavy rain 0.476 vs 0.961 |
| 6 | Fig 2 | 系统均值遮蔽相反站点异常 | k=2，silhouette=0.0914 |
| 7 | Vibe Coding | AI 扩展计算，研究者负责边界 | 责任矩阵+局限 |
| 8 | 假设与下阶段 | 补齐数据与识别，而非扩大结论 | H1/H2/H3 |

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 第 1 页｜研究问题与证据等级

**Assertion**：先用可审计 EDA 找到“哪里不同”，再把异常转成可证伪机制。  
**排版**：左侧研究链条；右侧卡片：17 天、239 trips、3 stations；加 synthetic winter validation 标签。  
**讲稿**：本次完成四类任务，重点是天气响应差异和空间异质性，不是年度总体结论。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 第 2 页｜数据源、许可与清洗漏斗

**Assertion**：质量损失小且可追踪，原始 Divvy 行程不进入交付文档。  
**排版**：Divvy、O'Hare USW00094846、Midway USW00014819 三卡片；漏斗 240→239，删除 1 行，删除率 0.417%、保留率 99.583%。  
**讲稿**：时间按 Chicago 本地时区，天气按日连接；raw/processed 均被 gitignore 排除。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 第 3 页｜站点 ID 修复与气象对齐

**Assertion**：不先修复 ID 漂移，跨期画像和异常会被人为拆散。  
**排版**：4 IDs 经 50 m Haversine 归并为 3 stations；突出 1 个漂移簇、2 个历史 ID。右侧写 PRCP 回补 1 日，最终缺失 0。  
**讲稿**：covid_trip_rows=0，240 条均为 2023_present；只能说明没有混池，不能做疫情前后比较。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 第 4 页｜统计分析：线索不等于显著

**Assertion**：温度与需求正相关，但短样本和多重检验使其仅为线索。  
**排版**：Pearson r=0.583、raw p=0.0141、FDR p=0.2532；Spearman ρ=0.496、FDR p=0.3851。红框：FDR<0.05 共 0 项，n=17 days。  
**讲稿**：原始 p 值不能越过 FDR；温度跨度 7.4°C，不能确认阈值。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 第 5 页｜Fig 1：天气响应由客群与车型共同塑造

**Assertion**：总体天气效应可能掩盖客群×车型韧性差异。  
**主图**：全宽放 ../outputs/figures/fig1_weather_nonlinear_response.png。  
**图 A 话术**：casual 弯点约 4.9°C、member 约 2.8°C，均为 exploratory，置信区间宽且不满足确认规则。  
**图 B 话术**：heavy rain 下 Casual-Classic 留存 47.6%、Casual-Electric 96.1%，但只有 2 个事件日；晴朗基准 1 天。图用于提出调节假设，不用于证明。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 第 6 页｜Fig 2：空间分群与同日相反异常

**Assertion**：同一天同类恶劣天气下，不同功能站可出现相反异常。  
**主图**：全宽放 ../outputs/figures/fig2_station_clusters_and_anomalies.png，并加小样本警示。  
**左图话术**：CBD 类 2 站/167 trips；Lakefront 类 1 站/72 trips。k=2 为回退，silhouette=0.0914，不可外推。  
**右图话术**：2024-01-13 Lakefront observed=0.0、expected=3.478、z=-4.406；同日 CBD 为正异常。下一阶段应估计 station×weather 异质性。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 第 7 页｜Vibe Coding：AI 扩展计算，研究者守住解释边界

**Assertion**：可信 Vibe Coding 是代码扩展性与人类判断责任的分工。  
**排版**：左侧 AI：CSV/GBFS、Haversine、NOAA 单位/插补、统计和多面板排版；右侧研究者：ID 业务漂移、2020 断点、机械/行为异常区分、机制假设。页底标签：短合成样本、3 stations、条件重叠、非因果。  
**讲稿**：AI 提高实现与复现效率；合并规则、断点、研究价值和识别策略由研究者主导。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>

## 第 8 页｜研究假设与下阶段实验预告

**Assertion**：下一阶段补齐每条机制缺口，而非扩大当前结论。  
**排版**：三列卡片“假设—缺数据—识别方法—失败判据”。  
1. **H1 车型调节**：天气×electric×>3 km×transit desert；补多年距离与 CTA/GTFS；station-hour 固定效应计数模型。
2. **H2 潮汐失衡**：小时 PRCP→晚高峰 net flow→次日空桩；补 NOAA ISD 与 GBFS status；窄窗事件研究。
3. **H3 韧性重构**：疫情阶段×cluster transition×weather elasticity；补疫情前后多年样本；固定特征转移矩阵与站点事件研究。

**收束讲稿**：H1 缺长距离和公共交通可达性，H2 缺小时降雨与库存，H3 缺疫情前后年份；下一次汇报围绕这些缺口验证。

<sub>Data Source: Divvy System Data & NOAA NCEI | Version/Accessed Date: 2026-11-09</sub>
