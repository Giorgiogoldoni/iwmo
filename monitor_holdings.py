#!/usr/bin/env python3
"""Download IWMO holdings from BlackRock and maintain dated snapshots/change reports."""
import csv, io, json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
import requests

URLS = [
 "https://www.blackrock.com/uk/professionals/en/products/270051/ishares-edge-msci-world-momentum-factor-ucits-etf/latest-holdings.csv",
 "https://www.blackrock.com/uk/individual/en/products/270051/ishares-edge-msci-world-momentum-factor-ucits-etf/latest-holdings.csv",
]
HEADERS = {"User-Agent":"Mozilla/5.0 (compatible; IWMO-Holdings-Monitor/1.0)", "Accept":"text/csv,*/*"}
ROOT=Path("holdings")
def get_csv():
    errors=[]
    for url in URLS:
        try:
            r=requests.get(url,headers=HEADERS,timeout=45)
            r.raise_for_status()
            if "html" in r.headers.get("content-type","").lower() or "Issuer Ticker" not in r.text:
                raise ValueError("response not recognized as holdings CSV")
            return r.text,url
        except Exception as e: errors.append(f"{url}: {e}")
    raise RuntimeError("Unable to retrieve official CSV. "+"; ".join(errors))

def parse(text):
    lines=text.splitlines()
    hidx=next((i for i,l in enumerate(lines) if "Issuer Ticker" in l and "Weight (%)" in l),None)
    if hidx is None: raise ValueError("CSV header row not found")
    reader=csv.DictReader(io.StringIO("\n".join(lines[hidx:])))
    rows=[]
    for raw in reader:
        if not raw or not raw.get("Issuer Ticker"): continue
        def clean(v):
            return (v or "").strip().strip('"')
        row={k:clean(v) for k,v in raw.items() if k}
        ticker=row.get("Issuer Ticker","")
        # Exclude footer/total rows, keep securities/cash where identifiable.
        if ticker.lower().startswith(("total","fund")): continue
        weight=row.get("Weight (%)","").replace(",","")
        try: row["weight_pct"]=float(weight)
        except: row["weight_pct"]=None
        row["ticker"]=ticker
        row["name"]=row.get("Name","")
        row["isin"]=row.get("ISIN","")
        rows.append(row)
    if len(rows)<100: raise ValueError(f"Unexpectedly small holdings list: {len(rows)} rows")
    return rows

def key(r):
    return r.get("isin") or r.get("ticker") or r.get("name")
def pct(r): return r.get("weight_pct") or 0
def compare(old,new):
    a={key(r):r for r in old}; b={key(r):r for r in new}
    adds=[b[k] for k in b.keys()-a.keys()]
    removes=[a[k] for k in a.keys()-b.keys()]
    changes=[]
    for k in a.keys()&b.keys():
        delta=pct(b[k])-pct(a[k])
        if abs(delta)>=0.01:
            changes.append({"ticker":b[k].get("ticker"),"name":b[k].get("name"),"old_weight":pct(a[k]),"new_weight":pct(b[k]),"delta_pp":round(delta,4)})
    changes.sort(key=lambda x:abs(x["delta_pp"]),reverse=True)
    return {"added":sorted(adds,key=lambda x:pct(x),reverse=True),"removed":sorted(removes,key=lambda x:pct(x),reverse=True),"weight_changes":changes}

def main():
    raw,url=get_csv(); holdings=parse(raw)
    now=datetime.now(timezone.utc); stamp=now.strftime("%Y%m%d")
    ROOT.mkdir(exist_ok=True); (ROOT/"snapshots").mkdir(exist_ok=True)
    snapshot={"downloaded_at":now.isoformat(),"source_url":url,"ticker":"IWMO","isin":"IE00BP3QZ825","count":len(holdings),"holdings":holdings}
    path=ROOT/"snapshots"/f"{stamp}.json"
    if path.exists():
        previous=json.loads(path.read_text())
        if previous.get("holdings")==holdings:
            print("Same-day snapshot unchanged"); return
    prevfiles=sorted((ROOT/"snapshots").glob("*.json"))
    prevfiles=[p for p in prevfiles if p!=path]
    previous=json.loads(prevfiles[-1].read_text()) if prevfiles else None
    diff=compare(previous["holdings"],holdings) if previous else {"added":[],"removed":[],"weight_changes":[]}
    snapshot["comparison_date"]=previous.get("downloaded_at") if previous else None
    snapshot["changes"]=diff
    path.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
    (ROOT/"latest.json").write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
    report={"as_of":now.isoformat(),"source":url,"previous_snapshot":snapshot["comparison_date"],"holding_count":len(holdings),"composition_changed":bool(diff["added"] or diff["removed"]),"added":[{"ticker":r.get("ticker"),"name":r.get("name"),"weight":pct(r)} for r in diff["added"]],"removed":[{"ticker":r.get("ticker"),"name":r.get("name"),"weight":pct(r)} for r in diff["removed"]],"weight_changes":diff["weight_changes"]}
    (ROOT/"changes.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(f"Saved {len(holdings)} holdings; additions {len(diff['added'])}; removals {len(diff['removed'])}; weight changes {len(diff['weight_changes'])}")
if __name__=="__main__": main()
