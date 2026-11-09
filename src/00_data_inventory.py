#!/usr/bin/env python3
"""Inventory Divvy and NOAA inputs."""
from __future__ import annotations
import argparse, json, logging, re, zipfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]; D=ROOT/"data/raw/divvy"; N=ROOT/"data/raw/noaa"; O=ROOT/"outputs/tables"
SOURCE="Divvy System Data & NOAA NCEI"
STATIONS={"USW00094846":"O'Hare (primary)","USW00014819":"Midway (backup)"}
METRICS={"PRCP":("mm",0,600),"SNWD":("mm",0,5000),"SNOW":("mm",0,1500),
 "AWND":("m/s",0,75),"TMAX":("°C",-65,60),"TMIN":("°C",-65,60)}
FIELDS=["ride_id","rideable_type","started_at","ended_at","start_station_name","start_station_id",
 "end_station_name","end_station_id","start_lat","start_lng","end_lat","end_lng","member_casual"]
ALIASES={"ride_id":("ride_id","trip_id"),"rideable_type":("rideable_type","bikeid"),
 "started_at":("started_at","start_time","starttime"),"ended_at":("ended_at","end_time","stoptime"),
 "start_station_name":("start_station_name","from_station_name"),"start_station_id":("start_station_id","from_station_id"),
 "end_station_name":("end_station_name","to_station_name"),"end_station_id":("end_station_id","to_station_id"),
 "start_lat":("start_lat","from_latitude"),"start_lng":("start_lng","start_lon","from_longitude"),
 "end_lat":("end_lat","to_latitude"),"end_lng":("end_lng","end_lon","to_longitude"),
 "member_casual":("member_casual","usertype","user_type")}
LOG=logging.getLogger("inventory")

def cli():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument("--synthetic-demo",action="store_true")
 p.add_argument("--force",action="store_true");p.add_argument("--chunk-size",type=int,default=250000)
 p.add_argument("--accessed-date",default=date.today().isoformat());a=p.parse_args()
 try:date.fromisoformat(a.accessed_date)
 except ValueError:p.error("--accessed-date must be YYYY-MM-DD")
 if a.chunk_size<1:p.error("--chunk-size must be positive")
 return a
def setup():
 for p in (D,N,ROOT/"data/processed",ROOT/"src",ROOT/"outputs/figures",O,ROOT/"docs"):p.mkdir(parents=True,exist_ok=True)
def sources(folder):
 out=[];js=[]
 for p in sorted(x for x in folder.rglob("*") if x.is_file()):
  if p.name.lower().endswith((".csv",".csv.gz")):out.append((p,None))
  elif p.name.lower().endswith(".zip"):
   try:
    with zipfile.ZipFile(p) as z:out += [(p,m) for m in z.namelist() if m.lower().endswith(".csv")]
   except zipfile.BadZipFile:LOG.warning("Invalid ZIP %s",p)
  elif p.name.lower().endswith((".json",".geojson")):js.append(p)
 return out,js
def frames(src,size):
 p,m=src;kw={"chunksize":size,"low_memory":False}
 if m:
  with zipfile.ZipFile(p) as z,z.open(m) as f:yield from pd.read_csv(f,**kw)
 else:yield from pd.read_csv(p,**kw)
def srcname(src):
 p,m=src;x=p.relative_to(ROOT).as_posix();return f"{x}::{m}" if m else x
def names(f):return {str(x).strip().lower():str(x) for x in f.columns}
def col(f,key):
 ns=names(f);return next((ns[x] for x in ALIASES[key] if x in ns),None)
def ncol(f,key):return next((str(x) for x in f.columns if re.match(rf"^{key}(?:\b|_)",str(x).strip(),re.I)),None)
def idset(x):x=x.dropna().astype("string").str.strip();return set(x[x.ne("")])
def coord(a,o):
 x=pd.DataFrame({"a":pd.to_numeric(a,errors="coerce"),"o":pd.to_numeric(o,errors="coerce")}).dropna()
 x=x[x.a.between(-90,90)&x.o.between(-180,180)];return set(zip(x.a.round(4),x.o.round(4)))
def regime(y):return "2019_pre_covid" if y<=2019 else ("2020_covid_shock" if y==2020 else ("2021_2022_recovery" if y<=2022 else "2023_present"))

class Divvy:
 def __init__(self):
  self.rows=0;self.miss=Counter({x:0 for x in FIELDS});self.ride=Counter();self.member=Counter();self.reg=Counter()
  self.ids={"start":set(),"end":set()};self.xy={"start":set(),"end":set()};self.low=self.high=None;self.badtime=0
 def add(self,f):
  n=len(f);self.rows+=n;c={x:col(f,x) for x in FIELDS}
  for k,v in c.items():self.miss[k]+=n if v is None else int(f[v].isna().sum())
  for v,counter in ((c["rideable_type"],self.ride),(c["member_casual"],self.member)):
   if v:counter.update(f[v].fillna("<MISSING>").astype(str).value_counts().to_dict())
  if c["started_at"]:
   t=pd.to_datetime(f[c["started_at"]],errors="coerce",format="mixed")
   t=t.dt.tz_localize("America/Chicago",ambiguous="NaT",nonexistent="shift_forward") if t.dt.tz is None else t.dt.tz_convert("America/Chicago")
   self.badtime+=int(t.isna().sum());v=t.dropna()
   if not v.empty:
    self.low=v.min() if self.low is None else min(self.low,v.min());self.high=v.max() if self.high is None else max(self.high,v.max())
    self.reg.update(v.dt.year.map(regime).value_counts().to_dict())
  for s in ("start","end"):
   if c[f"{s}_station_id"]:self.ids[s].update(idset(f[c[f"{s}_station_id"]]))
   if c[f"{s}_lat"] and c[f"{s}_lng"]:self.xy[s].update(coord(f[c[f"{s}_lat"]],f[c[f"{s}_lng"]]))

def synthetic(force):
 rng=np.random.default_rng(20261109);tp=D/"synthetic_divvy_trips.csv";gp=D/"synthetic_station_information.json";wp=N/"synthetic_noaa_daily_summaries.csv"
 if any(x.exists() for x in (tp,gp,wp)) and not force:raise FileExistsError("Synthetic fixtures exist; use --force")
 t=pd.date_range("2024-01-01 06:00",periods=240,freq="95min");sid=np.array(["SYN-A-v1","SYN-A-v2","SYN-B","SYN-C"])
 sn=np.array(["Synthetic Lake","Synthetic Lake","Synthetic Loop","Synthetic Park"]);lat=np.array([41.881,41.88102,41.8837,41.895]);lng=np.array([-87.623,-87.62301,-87.6324,-87.618])
 si,ei=np.random.default_rng(1).integers(0,4,len(t)),np.random.default_rng(2).integers(0,4,len(t));dur=pd.to_timedelta(rng.integers(5,55,len(t)),unit="m")
 f=pd.DataFrame({"ride_id":[f"SYNTHETIC-{i:06d}" for i in range(len(t))],"rideable_type":rng.choice(["classic_bike","electric_bike"],len(t)),
 "started_at":t.astype(str),"ended_at":(t+dur).astype(str),"start_station_name":sn[si],"start_station_id":sid[si],
 "end_station_name":sn[ei],"end_station_id":sid[ei],"start_lat":lat[si],"start_lng":lng[si],"end_lat":lat[ei],"end_lng":lng[ei],
 "member_casual":rng.choice(["member","casual"],len(t),p=[.72,.28])});f.loc[5,["end_station_id","end_lat","end_lng"]]=np.nan;f.to_csv(tp,index=False)
 gp.write_text(json.dumps({"last_updated":int(datetime.now(timezone.utc).timestamp()),"ttl":60,"version":"2.3",
  "data":{"stations":[{"station_id":str(sid[i]),"name":str(sn[i]),"lat":float(lat[i]),"lon":float(lng[i]),"capacity":15+3*i} for i in range(4)]}},indent=2),encoding="utf-8")
 rows=[];days=pd.date_range("2024-01-01","2024-01-17")
 for j,(station,label) in enumerate(STATIONS.items()):
  for i,day in enumerate(days):
   mean=-1.5+7*np.sin(i/5)+j*.7;rows.append({"STATION":station,"NAME":label,
    "LATITUDE":41.98-j*.20,"LONGITUDE":-87.90+j*.15,"ELEVATION":204.8-j*15,"DATE":day.date().isoformat(),
    "PRCP":round(max(0,rng.gamma(1.2,2)-1.5),1),"SNWD":round(max(0,22-i*1.8),1),"SNOW":round(max(0,rng.normal(1,2.5)),1),
    "AWND":round(rng.uniform(2,8.5),1),"TMAX":round(mean+3.5,1),"TMIN":round(mean-4.5,1)})
 wf=pd.DataFrame(rows);wf.loc[3,"PRCP"]=np.nan;wf.loc[len(days)+5,"AWND"]=np.nan;wf.to_csv(wp,index=False)

def scan(a):
 d=Divvy();assets=[];station_assets=[]
 dsrc,djson=sources(D);nsrc,_=sources(N)
 for s in dsrc:
  try:
   sample=next(frames(s,20));ns=set(names(sample));kind="divvy_trip" if col(sample,"started_at") else ("divvy_station" if ns&{"station_id","id"} else "unknown")
   if kind=="divvy_trip":
    before=d.rows
    for f in frames(s,a.chunk_size):d.add(f)
    rows=d.rows-before
   elif kind=="divvy_station":
    rows=0;station_ids=set();station_xy=set()
    for f in frames(s,a.chunk_size):
     rows+=len(f);ns=names(f);ic=next((ns[x] for x in ("station_id","id","short_name") if x in ns),None)
     la=next((ns[x] for x in ("lat","latitude") if x in ns),None);lo=next((ns[x] for x in ("lon","lng","longitude") if x in ns),None)
     if ic:station_ids.update(idset(f[ic]))
     if la and lo:station_xy.update(coord(f[la],f[lo]))
    station_assets.append({"asset":srcname(s),"rows":rows,"station_id_unique":len(station_ids),"coordinate_4dp_unique":len(station_xy)})
   else:rows=sum(len(f) for f in frames(s,a.chunk_size))
   assets.append(asset(s,kind,rows));LOG.info("%s: %s rows",srcname(s),f"{rows:,}")
  except Exception as e:LOG.exception("Failed %s",srcname(s));assets.append(asset(s,"ERROR",0,str(e)))
 for p in djson:
  try:
   q=json.loads(p.read_text());rows=q.get("data",q).get("stations",[])
   f=pd.DataFrame(rows);ns=names(f);i=ns.get("station_id");la=ns.get("lat");lo=ns.get("lon")
   station_assets.append({"asset":p.relative_to(ROOT).as_posix(),"rows":len(f),
    "station_id_unique":len(idset(f[i])) if i else 0,"coordinate_4dp_unique":len(coord(f[la],f[lo])) if la and lo else 0})
   assets.append(asset((p,None),"divvy_station_gbfs",len(f)))
  except Exception as e:assets.append(asset((p,None),"ERROR",0,str(e)))
 states={x:{"rows":0,"dates":[],"miss":Counter({m:0 for m in METRICS}),"bounds":{m:[] for m in METRICS},"bad":0} for x in STATIONS}
 for s in nsrc:
  try:
   rows=0
   for f in frames(s,a.chunk_size):
    rows+=len(f);sc=ncol(f,"STATION")
    for station in STATIONS:
     part=f.loc[f[sc].astype("string").str.strip().eq(station)] if sc else f.iloc[0:0]
     st=states[station];st["rows"]+=len(part);dc=ncol(part,"DATE")
     if dc:
      z=pd.to_datetime(part[dc],errors="coerce").dropna()
      if not z.empty:st["dates"] += [z.min(),z.max()]
     vals={}
     for m in METRICS:
      c=ncol(part,m);z=pd.to_numeric(part[c],errors="coerce") if c else pd.Series(np.nan,index=part.index)
      vals[m]=z;st["miss"][m]+=int(z.isna().sum())
      if z.notna().any():st["bounds"][m] += [float(z.min()),float(z.max())]
     ok=vals["TMAX"].notna()&vals["TMIN"].notna();st["bad"]+=int((vals["TMAX"][ok]<vals["TMIN"][ok]).sum())
   assets.append(asset(s,"noaa_weather",rows));LOG.info("%s: %s rows",srcname(s),f"{rows:,}")
  except Exception as e:LOG.exception("Failed %s",srcname(s));assets.append(asset(s,"ERROR",0,str(e)))
 missing=pd.DataFrame([{"field":x,"missing_count":d.miss[x],"missing_pct":round(100*d.miss[x]/d.rows,4) if d.rows else np.nan} for x in FIELDS])
 compare=pd.DataFrame([{"scope":s,"station_id_unique":len(d.ids[s]),"coordinate_4dp_unique":len(d.xy[s]),
  "id_minus_coordinate":len(d.ids[s])-len(d.xy[s])} for s in ("start","end")])
 spans=[];quality=[]
 for station,role in STATIONS.items():
  st=states[station];dates=st["dates"];spans.append({"station":station,"rows":st["rows"],
   "date_min":min(dates).date().isoformat() if dates else None,"date_max":max(dates).date().isoformat() if dates else None,
   "tmax_lt_tmin_rows":st["bad"]})
  for m,(unit,lo,hi) in METRICS.items():
   b=st["bounds"][m];vmin,vmax=(min(b),max(b)) if b else (None,None)
   status="NO_DATA" if vmin is None else ("PASS: SI-plausible" if vmin>=lo and vmax<=hi else "FAIL: outside SI range")
   quality.append({"station":station,"role":role,"metric":m,"expected_unit":unit,"rows":st["rows"],
    "missing_count":st["miss"][m],"missing_pct":round(100*st["miss"][m]/st["rows"],4) if st["rows"] else np.nan,
    "observed_min":vmin,"observed_max":vmax,"unit_plausibility":status})
 return d,{"assets":pd.DataFrame(assets),"missing":missing,"compare":compare,"spans":pd.DataFrame(spans),
  "quality":pd.DataFrame(quality),"station_assets":pd.DataFrame(station_assets),
  "rideable":dist(d.ride,d.rows,"rideable_type"),"member":dist(d.member,d.rows,"member_casual"),"regimes":dist(d.reg,d.rows,"regime")}

def asset(src,kind,rows,note=""):
 p,_=src;st=p.stat();return {"asset":srcname(src),"type":kind,"rows":int(rows),"size_bytes":st.st_size,
  "modified_utc":datetime.fromtimestamp(st.st_mtime,timezone.utc).isoformat(),"note":note}
def dist(c,total,label):
 return pd.DataFrame([{label:str(k),"count":v,"share_pct":round(100*v/total,4) if total else np.nan} for k,v in c.most_common()],columns=[label,"count","share_pct"])
def md(f,a):
 table="_无可用记录。_" if f.empty else f.fillna("NA").to_markdown(index=False)
 return f"{table}\n\n<sub>Data Source: {SOURCE} | Version/Accessed Date: {a}</sub>\n"
def iso(x):return None if x is None else pd.Timestamp(x).isoformat()

def output(a,d,f):
 for name,key in (("00_asset_inventory.csv","assets"),("00_divvy_missingness.csv","missing"),("00_station_id_coordinate_comparison.csv","compare"),("00_noaa_quality.csv","quality")):
  x=f[key].copy();x["data_source"]=SOURCE;x["version_accessed_date"]=a.accessed_date;x.to_csv(O/name,index=False)
 versions=([d.high.date()] if d.high is not None else [])+[date.fromisoformat(x) for x in f["spans"].date_max.dropna()]
 version=max(versions).isoformat() if versions else "unknown";generated=datetime.now(timezone.utc).isoformat()
 summary={"generated_at_utc":generated,"accessed_date":a.accessed_date,"inferred_data_version_date":version,"data_source":SOURCE,
  "divvy":{"rows":d.rows,"started_at_min_chicago":iso(d.low),"started_at_max_chicago":iso(d.high),"invalid_started_at":d.badtime,
  "missingness":dict(d.miss),"rideable_type":dict(d.ride),"member_casual":dict(d.member),"regime_counts":dict(d.reg)},
  "noaa":f["spans"].to_dict(orient="records")}
 (O/"00_data_inventory_metrics.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
 overview=pd.DataFrame([{"total_rows":d.rows,"started_at_min_chicago":iso(d.low),"started_at_max_chicago":iso(d.high),"invalid_started_at":d.badtime}])
 report=["# 数据资产与质量初筛报告","",f"- 生成时间（UTC）：`{generated}`",f"- 数据访问日期：`{a.accessed_date}`",f"- 数据版本日期：`{version}`",
  "- Divvy 时间按 `America/Chicago` 解析；仅输出聚合指标。","","## 1. 数据资产清单","",md(f["assets"],a.accessed_date),
  "## 2. Divvy 时间跨度与规模","",md(overview,a.accessed_date),"### 字段缺失率","",md(f["missing"],a.accessed_date),
  "### `rideable_type` 分布","",md(f["rideable"],a.accessed_date),"### `member_casual` 分布","",md(f["member"],a.accessed_date),
  "### COVID-19 阶段","",md(f["regimes"],a.accessed_date),"2020 数据必须作为结构断点单列。","",
  "## 3. 站点 ID 稳定性","","坐标按 4 位小数形成 `canonical_station_id` 候选。","",md(f["compare"],a.accessed_date),
  "### GBFS/站点资产","",md(f["station_assets"],a.accessed_date),"## 4. NOAA 主/备站量纲检查","",
  "合理性检查不替代 NCEI `units=metric` 元数据核验。","",md(f["spans"],a.accessed_date),md(f["quality"],a.accessed_date),
  "## 5. 下一步","","1. 修复 FAIL 后再合并。","2. 用 4 位坐标或 50 米 Haversine 聚类 harmonize 站点。",
  "3. 记录清洗 shape/drop rate。","",f"<sub>Data Source: {SOURCE} | Version/Accessed Date: {a.accessed_date}</sub>"]
 (O/"00_data_inventory_report.md").write_text("\n".join(report),encoding="utf-8")

def main():
 logging.basicConfig(level=logging.INFO,format="%(levelname)s | %(message)s");a=cli();setup()
 if a.synthetic_demo:synthetic(a.force)
 d,f=scan(a);output(a,d,f);LOG.info("Done: %s Divvy rows; report=%s",f"{d.rows:,}",O/"00_data_inventory_report.md")
if __name__=="__main__":
 try:main()
 except FileExistsError as e:LOG.error("%s",e);raise SystemExit(2)
