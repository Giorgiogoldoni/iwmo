#!/usr/bin/env python3
"""Archive BlackRock IWMO holdings, changes, workbook and transparent momentum proxy."""
import csv, io, json
from datetime import datetime, timezone
from pathlib import Path
import requests

URLS=[
 "https://www.ishares.com/uk/individual/en/products/270051/ishares-msci-world-momentum-factor-ucits-etf-iwmo/latest-holdings.csv",
 "https://www.blackrock.com/uk/individual/en/products/270051/ishares-msci-world-momentum-factor-ucits-etf-iwmo/latest-holdings.csv",
]
API_URL="https://www.ishares.com/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data"
HEADERS={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36","Accept":"text/csv,application/json,*/*","Referer":"https://www.ishares.com/uk/individual/en/products/270051/ishares-msci-world-momentum-factor-ucits-etf-iwmo"}
ROOT=Path("holdings"); SNAP=ROOT/"snapshots"; RAW=ROOT/"raw_csv"

def api_csv():
 # BlackRock's current product-data JSON plane; fallback IDs are product ID and known fund portfolio IDs.
 for pid in ("270051","270050","270052"):
  params={"appSubType":"ISHARES","appType":"PRODUCT_PAGE","component":"holdings.all","locale":"en_US","portfolioId":pid,"targetSite":"uk-ishares","userType":"individual","excludeContent":"true"}
  try:
   r=requests.get(API_URL,params=params,headers=HEADERS,timeout=40);r.raise_for_status(); j=r.json()
   dp=j.get("componentsByNameMap",{}).get("holdings",{}).get("containersByNameMap",{}).get("all",{}).get("dataPointsByNameMap",{})
   cols={"Issuer Ticker":"ticker","Name":"issueName","Sector":"sectorName","Asset Class":"assetClass","Market Value":"marketValue","Weight (%)":"holdingPercent","Notional Value":"notionalValue","Nominal":"unitsHeld","ISIN":"isin","Market Currency":"currencyCode"}
   rows=[]
   n=max((len(dp.get(src,{}).get("value",[])) for src in cols.values()),default=0)
   if n<100: continue
   for i in range(n):
    rows.append({dst:(dp.get(src,{}).get("value",[])[i] if i<len(dp.get(src,{}).get("value",[])) else "") for dst,src in cols.items()})
   buf=io.StringIO();w=csv.DictWriter(buf,fieldnames=list(cols));w.writeheader();w.writerows(rows)
   return buf.getvalue(),r.url
  except Exception as e: print("API attempt",pid,"failed:",e)
 return None

def rebalix_csv():
 try:
  import pandas as pd
  url="https://rebalix.com/api/etf/composizione?isin=IE00BP3QZ825&lang=en"
  r=requests.get(url,headers=HEADERS,timeout=60);r.raise_for_status()
  if not r.content.startswith(b"PK"): raise ValueError("Rebalix download did not return XLSX")
  x=pd.read_excel(io.BytesIO(r.content),sheet_name=0,header=None)
  # Detect the real column-header row (download contains a short metadata block).
  header_row=None
  for i in range(min(12,len(x))):
   vals=[str(v).strip().lower() for v in x.iloc[i].tolist()]
   if any("ticker" in v or "symbol" in v for v in vals) and any("weight" in v or "percent" in v or "%" in v for v in vals):
    header_row=i;break
  if header_row is None: raise ValueError("Unable to identify holdings columns in XLSX")
  df=pd.read_excel(io.BytesIO(r.content),sheet_name=0,header=header_row)
  df=df.dropna(how="all")
  # Keep the third-party source's native fields; normalize recognized fields for parser.
  cols={str(k).strip():k for k in df.columns}
  ticker_col=next((k for k in df.columns if any(x in str(k).lower() for x in ["ticker","symbol"])),None)
  weight_col=next((k for k in df.columns if any(x in str(k).lower() for x in ["weight","percent","%"])),None)
  name_col=next((k for k in df.columns if any(x in str(k).lower() for x in ["name","company","issuer"])),None)
  if ticker_col is None or weight_col is None: raise ValueError("XLSX columns not compatible")
  out=pd.DataFrame({"Issuer Ticker":df[ticker_col].astype(str),"Name":df[name_col].astype(str) if name_col is not None else "", "Weight (%)":df[weight_col]})
  out=out[out["Issuer Ticker"].str.len().between(1,20)]
  if len(out)<100: raise ValueError(f"Too few rows in fallback file: {len(out)}")
  buf=io.StringIO();out.to_csv(buf,index=False)
  return buf.getvalue(),url
 except Exception as e:
  print("Rebalix fallback failed:",repr(e));return None

def get_csv():
 errors=[]
 api=api_csv()
 if api:return api
 for url in URLS:
  try:
   r=requests.get(url,headers=HEADERS,timeout=45);r.raise_for_status()
   if "<html" in r.text[:500].lower() or not any(x in r.text for x in ["Weight (%)","Weight","Ticker"]): raise ValueError("not recognized holdings CSV")
   return r.text,url
  except Exception as e: errors.append(f"{url}: {e}")
 raise RuntimeError("Cannot fetch iShares holdings: JSON API and CSV endpoints failed; "+"; ".join(errors))
def parse(text):
 lines=text.splitlines(); hidx=next((i for i,l in enumerate(lines) if ("Issuer Ticker" in l or "Ticker" in l) and ("Weight (%)" in l or "Weight" in l)),None)
 if hidx is None: raise ValueError("CSV header missing")
 rows=[]
 for raw in csv.DictReader(io.StringIO("\n".join(lines[hidx:]))):
  if not raw or not raw.get("Issuer Ticker"): continue
  row={k:(v or "").strip().strip('"') for k,v in raw.items() if k}
  ticker=row.get("Issuer Ticker") or row.get("Ticker") or row.get("Symbol") or ""
  if ticker.lower().startswith(("total","fund")): continue
  try: row["weight_pct"]=float((row.get("Weight (%)") or row.get("Weight") or row.get("Portfolio Weight") or "").replace(",",""))
  except: row["weight_pct"]=None
  row.update(ticker=ticker,name=row.get("Name",""),isin=row.get("ISIN",""))
  rows.append(row)
 if len(rows)<100: raise ValueError(f"Too few rows: {len(rows)}")
 return rows

def key(r): return r.get("isin") or r.get("ticker") or r.get("name")
def pct(r): return r.get("weight_pct") or 0
def compare(old,new):
 a={key(r):r for r in old};b={key(r):r for r in new}
 adds=[b[k] for k in b.keys()-a.keys()];removes=[a[k] for k in a.keys()-b.keys()];changes=[]
 for k in a.keys()&b.keys():
  delta=pct(b[k])-pct(a[k])
  if abs(delta)>=0.01: changes.append({"ticker":b[k].get("ticker"),"name":b[k].get("name"),"old_weight":pct(a[k]),"new_weight":pct(b[k]),"delta_pp":round(delta,4)})
 changes.sort(key=lambda x:abs(x["delta_pp"]),reverse=True)
 return {"added":sorted(adds,key=pct,reverse=True),"removed":sorted(removes,key=pct,reverse=True),"weight_changes":changes}

def yahoo_ticker(t): return (t or "").strip().replace(" ","-")

def momentum_for(tickers):
 """Approximate MSCI-style momentum components on current holdings only.
 Uses 6/12m returns excluding latest month and 3y weekly volatility.
 Risk-free adjustment and parent-index universe z-scoring are unavailable; not official MSCI score.
 """
 result={}
 try:
  import numpy as np
  import yfinance as yf
  symbols=list(dict.fromkeys(yahoo_ticker(t) for t in tickers if t))
  data=yf.download(symbols,period="5y",interval="1d",auto_adjust=True,group_by="ticker",threads=True,progress=False)
  if data.empty:return result
  raw={}
  for sym in symbols:
   try:
    close=data[sym]["Close"].dropna() if len(symbols)>1 else data["Close"].dropna()
    if len(close)<300:continue
    endp=close.iloc[-22] # approximately T-1 month
    p6=close.iloc[-148] if len(close)>=148 else None
    p12=close.iloc[-274] if len(close)>=274 else None
    ret6=float(endp/p6-1) if p6 is not None else None
    ret12=float(endp/p12-1) if p12 is not None else None
    weekly=close.resample("W-FRI").last().dropna().pct_change().dropna()
    vol3=float(weekly.iloc[-156:].std()*52**0.5) if len(weekly)>=130 else None
    ra6=ret6/vol3 if ret6 is not None and vol3 and vol3>0 else None
    ra12=ret12/vol3 if ret12 is not None and vol3 and vol3>0 else None
    raw[sym]={"momentum_6m_pct":round(ret6*100,2) if ret6 is not None else None,
     "momentum_12m_pct":round(ret12*100,2) if ret12 is not None else None,
     "volatility_3y_weekly_pct":round(vol3*100,2) if vol3 is not None else None,
     "risk_adjusted_6m":ra6,"risk_adjusted_12m":ra12,"momentum_data_date":str(close.index[-22].date())}
   except Exception:continue
  def zvals(field):
   vals={k:v[field] for k,v in raw.items() if v.get(field) is not None}
   if len(vals)<2:return {k:0.0 for k in vals}
   mean=float(np.mean(list(vals.values())));std=float(np.std(list(vals.values()),ddof=0))
   return {k:((v-mean)/std if std else 0.0) for k,v in vals.items()}
  z6=zvals("risk_adjusted_6m");z12=zvals("risk_adjusted_12m")
  combined={k:0.5*z6.get(k,0)+0.5*z12.get(k,0) for k in raw if k in z6 or k in z12}
  if combined:
   mean=float(np.mean(list(combined.values())));std=float(np.std(list(combined.values()),ddof=0))
   for k,v in combined.items():
    z=max(-3,min(3,(v-mean)/std if std else 0));score=1+z if z>0 else 1/(1-z)
    raw[k].update({"momentum_z":round(z,4),"momentum_proxy":round(score,4)})
  result=raw
 except Exception as e:print("Momentum fetch unavailable:",repr(e))
 return result

def write_workbook(snapshot,snapshots,mom):
 import pandas as pd
 current=pd.DataFrame(snapshot["holdings"])
 current["yahoo_symbol"]=current["ticker"].map(yahoo_ticker)
 for field in ["momentum_6m_pct","momentum_12m_pct","volatility_3y_weekly_pct","momentum_z","momentum_proxy","momentum_data_date"]:
  current[field]=current["yahoo_symbol"].map(lambda s:mom.get(s,{}).get(field))
 current["momentum_rank"]=current["momentum_proxy"].rank(ascending=False,method="min")
 ch=snapshot.get("changes",{})
 hist=[{"date":s.get("downloaded_at","")[:10],"ticker":r.get("ticker"),"isin":r.get("isin"),"name":r.get("name"),"weight_pct":r.get("weight_pct"),"sector":r.get("Sector")} for s in snapshots for r in s.get("holdings",[])]
 hist=pd.DataFrame(hist)
 pivot=hist.pivot_table(index=["ticker","isin","name"],columns="date",values="weight_pct",aggfunc="last").reset_index() if not hist.empty else pd.DataFrame()
 method=pd.DataFrame([["Source","Official BlackRock/iShares CSV"],["6m momentum","Adjusted price return approx 126 sessions"],["12m momentum","Adjusted price return approx 252 sessions"],["Volatility","Annualized trailing daily-return standard deviation approx 252 sessions"],["Proxy score","Mean of 6m and 12m returns divided by annualized volatility; NOT official MSCI score"],["Universe","Only current holdings, not full parent index universe; cannot verify inclusion decisions"],["Limitations","Yahoo ticker matching, missing series and cross-currency effects; proxy does not replicate MSCI official calculation"],["Snapshot",snapshot.get("downloaded_at","")]],columns=["Item","Definition / limitation"])
 out=ROOT/"IWMO_holdings_history.xlsx"
 with pd.ExcelWriter(out,engine="openpyxl") as w:
  current.to_excel(w,index=False,sheet_name="Current holdings")
  hist.to_excel(w,index=False,sheet_name="Daily raw history")
  pivot.to_excel(w,index=False,sheet_name="Weight history")
  pd.DataFrame(ch.get("added",[])).to_excel(w,index=False,sheet_name="Added")
  pd.DataFrame(ch.get("removed",[])).to_excel(w,index=False,sheet_name="Removed")
  pd.DataFrame(ch.get("weight_changes",[])).to_excel(w,index=False,sheet_name="Weight changes")
  method.to_excel(w,index=False,sheet_name="Momentum method")
  for ws in w.book.worksheets:
   ws.freeze_panes="A2";ws.auto_filter.ref=ws.dimensions
   for col in ws.columns:
    letter=col[0].column_letter;maxlen=max((len(str(c.value or "")) for c in col[:250]),default=10)
    ws.column_dimensions[letter].width=min(max(maxlen+2,12),38)
 return out

def main():
 raw,url=get_csv();holdings=parse(raw);now=datetime.now(timezone.utc);stamp=now.strftime("%Y%m%d")
 SNAP.mkdir(parents=True,exist_ok=True);RAW.mkdir(parents=True,exist_ok=True)
 (RAW/f"{stamp}.csv").write_text(raw,encoding="utf-8")
 snapshot={"downloaded_at":now.isoformat(),"source_url":url,"ticker":"IWMO","isin":"IE00BP3QZ825","count":len(holdings),"holdings":holdings}
 path=SNAP/f"{stamp}.json";previous_files=sorted(p for p in SNAP.glob("*.json") if p!=path)
 previous=json.loads(previous_files[-1].read_text()) if previous_files else None
 diff=compare(previous["holdings"],holdings) if previous else {"added":[],"removed":[],"weight_changes":[]}
 snapshot["comparison_date"]=previous.get("downloaded_at") if previous else None;snapshot["changes"]=diff
 path.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
 (ROOT/"latest.json").write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
 report={"as_of":now.isoformat(),"source":url,"previous_snapshot":snapshot["comparison_date"],"holding_count":len(holdings),"composition_changed":bool(diff["added"] or diff["removed"]),"added":[{"ticker":r.get("ticker"),"name":r.get("name"),"weight":pct(r)} for r in diff["added"]],"removed":[{"ticker":r.get("ticker"),"name":r.get("name"),"weight":pct(r)} for r in diff["removed"]],"weight_changes":diff["weight_changes"]}
 (ROOT/"changes.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print("Fetching approximate momentum metrics for constituents…")
 mom=momentum_for([r.get("ticker") for r in holdings])
 for r in holdings:
  m=mom.get(yahoo_ticker(r.get("ticker")),{});r.update(m)
 ranked=sorted([(r.get("momentum_proxy"),r) for r in holdings if r.get("momentum_proxy") is not None],reverse=True,key=lambda x:x[0])
 for rank,(_,r) in enumerate(ranked,1):r["momentum_rank"]=rank
 snapshot["momentum_note"]="Yahoo-derived approximate proxy; not MSCI's official methodology."
 snapshot["momentum_coverage"]=len(mom);snapshot["momentum"]=mom
 path.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2));(ROOT/"latest.json").write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
 all_snaps=[json.loads(p.read_text()) for p in sorted(SNAP.glob("*.json"))]
 write_workbook(snapshot,all_snaps,mom)
 print(f"Archived {len(holdings)} holdings; momentum data for {len(mom)} symbols; {len(diff['added'])} additions, {len(diff['removed'])} removals.")
if __name__=="__main__":main()
