#!/usr/bin/env python3
"""Archive BlackRock IWMO holdings, changes, workbook and transparent momentum proxy."""
import csv, io, json
from datetime import datetime, timezone
from pathlib import Path
import requests

URLS=[
 "https://www.blackrock.com/uk/professionals/en/products/270051/ishares-edge-msci-world-momentum-factor-ucits-etf/latest-holdings.csv",
 "https://www.blackrock.com/uk/individual/en/products/270051/ishares-edge-msci-world-momentum-factor-ucits-etf/latest-holdings.csv",
]
HEADERS={"User-Agent":"Mozilla/5.0 (compatible; IWMO-Holdings-Monitor/1.0)","Accept":"text/csv,*/*"}
ROOT=Path("holdings"); SNAP=ROOT/"snapshots"; RAW=ROOT/"raw_csv"

def get_csv():
 errors=[]
 for url in URLS:
  try:
   r=requests.get(url,headers=HEADERS,timeout=45);r.raise_for_status()
   if "html" in r.headers.get("content-type","").lower() or "Issuer Ticker" not in r.text: raise ValueError("not recognized holdings CSV")
   return r.text,url
  except Exception as e: errors.append(f"{url}: {e}")
 raise RuntimeError("Cannot fetch BlackRock holdings CSV: "+"; ".join(errors))

def parse(text):
 lines=text.splitlines(); hidx=next((i for i,l in enumerate(lines) if "Issuer Ticker" in l and "Weight (%)" in l),None)
 if hidx is None: raise ValueError("CSV header missing")
 rows=[]
 for raw in csv.DictReader(io.StringIO("\n".join(lines[hidx:]))):
  if not raw or not raw.get("Issuer Ticker"): continue
  row={k:(v or "").strip().strip('"') for k,v in raw.items() if k}
  ticker=row.get("Issuer Ticker","")
  if ticker.lower().startswith(("total","fund")): continue
  try: row["weight_pct"]=float(row.get("Weight (%)","").replace(",",""))
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
 """Yahoo proxy: 6m/12m adjusted returns and annualized volatility; not MSCI official score."""
 result={}
 try:
  import yfinance as yf
  symbols=list(dict.fromkeys(yahoo_ticker(t) for t in tickers if t))
  data=yf.download(symbols,period="2y",interval="1d",auto_adjust=True,group_by="ticker",threads=True,progress=False)
  if data.empty:return result
  for sym in symbols:
   try:
    close=data[sym]["Close"].dropna() if len(symbols)>1 else data["Close"].dropna()
    if len(close)<190:continue
    r6=float(close.iloc[-1]/close.iloc[-126]-1) if len(close)>=126 else None
    r12=float(close.iloc[-1]/close.iloc[-252]-1) if len(close)>=252 else None
    daily=close.pct_change().dropna();vol=float(daily.iloc[-252:].std()*252**0.5) if len(daily)>=200 else None
    composite=(0.5*r6+0.5*r12) if r6 is not None and r12 is not None else None
    score=composite/vol if composite is not None and vol and vol>0 else None
    result[sym]={"momentum_6m_pct":round(r6*100,2) if r6 is not None else None,"momentum_12m_pct":round(r12*100,2) if r12 is not None else None,"volatility_12m_pct":round(vol*100,2) if vol is not None else None,"momentum_proxy":round(score,4) if score is not None else None,"momentum_data_date":str(close.index[-1].date())}
   except Exception:continue
 except Exception as e: print("Momentum fetch unavailable:",repr(e))
 return result

def write_workbook(snapshot,snapshots,mom):
 import pandas as pd
 current=pd.DataFrame(snapshot["holdings"])
 current["yahoo_symbol"]=current["ticker"].map(yahoo_ticker)
 for field in ["momentum_6m_pct","momentum_12m_pct","volatility_12m_pct","momentum_proxy","momentum_data_date"]:
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
