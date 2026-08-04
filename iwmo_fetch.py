#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAPTOR IWMO — Data Fetch
Scarica dati iShares World Momentum UCITS ETF (IWMO.MI)
Calcola: stagionalità, momentum Antonacci, indicatori RAPTOR, livelli supporto

Schedule:
- 05:30 CET: Analisi completa notturna + aggiornamento storico
- 16:45 CET: Rilevazione intra-day (segnali aggiornati)
- 17:00 CET: Chiusura giornaliera + salvataggio completo
"""

import json, math, os
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import yfinance as yf

# ── RILEVAMENTO ORARIO ─────────────────────────────────
def get_execution_type():
    """Determina il tipo di esecuzione basato sull'orario UTC"""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    minute = now_utc.minute
    
    # 05:30 CET = 04:30 UTC
    if 4 <= hour < 5 or (hour == 4 and minute >= 30):
        return 'morning'
    # 16:45 CET = 15:45 UTC
    elif 15 <= hour < 16 or (hour == 15 and minute >= 45):
        return 'intraday'
    # 17:00 CET = 16:00 UTC
    elif 16 <= hour < 17 or (hour == 16 and minute >= 0):
        return 'close'
    else:
        return 'manual'

# ── INDICATORI ────────────────────────────────────────
def calc_kama(closes, n=10, fast=2, slow=30):
    fsc = 2/(fast+1); ssc = 2/(slow+1)
    kama = [None]*len(closes)
    if len(closes) <= n: return kama
    kama[n] = closes[n]
    for i in range(n+1, len(closes)):
        d = abs(closes[i]-closes[i-n])
        v = sum(abs(closes[j]-closes[j-1]) for j in range(i-n+1, i+1))
        er = d/v if v else 0
        sc = (er*(fsc-ssc)+ssc)**2
        kama[i] = kama[i-1] + sc*(closes[i]-kama[i-1])
    return kama

def calc_rsi(closes, n=14):
    res = [None]*len(closes)
    for i in range(n+1, len(closes)):
        gs=[]; ls=[]
        for j in range(i-n, i+1):
            dd = closes[j]-closes[j-1]
            gs.append(max(dd,0)); ls.append(max(-dd,0))
        ag=sum(gs)/n; al=sum(ls)/n
        res[i] = round(100-100/(1+ag/al),2) if al>0 else 100.0
    return res

def calc_ao(highs, lows):
    mid = [(h+l)/2 for h,l in zip(highs,lows)]
    def ema(arr, p):
        k=2/(p+1); e=arr[0]; out=[e]
        for x in arr[1:]: e=x*k+e*(1-k); out.append(e)
        return out
    if len(mid)<13: return [0]*len(mid)
    e3=ema(mid,3); e13=ema(mid,13)
    return [round(a-b,4) for a,b in zip(e3,e13)]

def calc_sar(high, low, step=0.02, max_af=0.20):
    n=len(high); sar=[None]*n
    if n<5: return sar
    bull=high[1]>high[0]; af=step
    ep=max(high[:2]) if bull else min(low[:2])
    sar[1]=min(low[:2]) if bull else max(high[:2])
    for i in range(2,n):
        ps=sar[i-1]
        if bull:
            sar[i]=min(ps+af*(ep-ps), low[i-1], low[i-2] if i>=2 else low[i-1])
            if low[i]<sar[i]: bull=False; af=step; sar[i]=ep; ep=low[i]
            else:
                if high[i]>ep: ep=high[i]; af=min(af+step,max_af)
        else:
            sar[i]=max(ps+af*(ep-ps), high[i-1], high[i-2] if i>=2 else high[i-1])
            if high[i]>sar[i]: bull=True; af=step; sar[i]=ep; ep=high[i]
            else:
                if low[i]<ep: ep=low[i]; af=min(af+step,max_af)
    return sar

def calc_er(closes, n=10):
    res=[0]*len(closes)
    for i in range(n,len(closes)):
        d=abs(closes[i]-closes[i-n])
        v=sum(abs(closes[j]-closes[j-1]) for j in range(i-n+1,i+1))
        res[i]=round(d/v,4) if v else 0
    return res

def sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj): return None
        return obj
    if isinstance(obj, dict): return {k:sanitize(v) for k,v in obj.items()}
    if isinstance(obj, list): return [sanitize(v) for v in obj]
    return obj

# ── STAGIONALITÀ 25 ANNI / STORICA ────────────────────
def calc_stagionalita(closes, dates):
    """Rendimento medio mensile storico"""
    monthly_rets = defaultdict(list)
    for i in range(1, len(closes)):
        if closes[i] and closes[i-1]:
            month = int(dates[i][5:7])
            ret = (closes[i]-closes[i-1])/closes[i-1]*100
            monthly_rets[month].append(ret)

    stagionalita = []
    mesi = ['Gen','Feb','Mar','Apr','Mag','Giu','Lug','Ago','Set','Ott','Nov','Dic']
    for m in range(1,13):
        rets = monthly_rets[m]
        avg = sum(rets)/len(rets) if rets else 0
        positive = sum(1 for r in rets if r>0)
        wr = positive/len(rets)*100 if rets else 0
        stagionalita.append({
            'mese': m,
            'nome': mesi[m-1],
            'avg_ret': round(avg,3),
            'win_rate': round(wr,1),
            'n_anni': len(rets)
        })
    return stagionalita

# ── SUPPORTI ─────────────────────────────────────────
def find_supports(closes, dates, window=10, cluster_pct=0.01, top_n=5):
    """Individua i minimi locali su una finestra ampia (default ±10gg, minimi
    di respiro settimanale/mensile, non rumore di 2-3 giorni), poi raggruppa
    i minimi entro cluster_pct l'uno dall'altro nello stesso livello
    (touch count = quante volte quel livello è stato toccato). Ritorna i
    top_n livelli più significativi, ordinati per un punteggio misto:
    numero di tocchi + vicinanza al prezzo attuale (i supporti più vicini e
    più testati contano di più di un minimo isolato e lontano nel tempo)."""
    raw = []
    for i in range(window, len(closes)-window):
        if not closes[i]: continue
        is_min = all(closes[i] <= closes[i-j] for j in range(1,window+1) if closes[i-j]) and \
                 all(closes[i] <= closes[i+j] for j in range(1,window+1) if closes[i+j])
        if is_min:
            raw.append({'date': dates[i], 'price': closes[i]})
    if not raw:
        return []

    # ── clustering per livello di prezzo ──
    raw.sort(key=lambda s: s['price'])
    clusters = []
    for s in raw:
        placed = False
        for c in clusters:
            if abs(s['price'] - c['level']) / c['level'] <= cluster_pct:
                c['touches'].append(s)
                c['level'] = sum(t['price'] for t in c['touches']) / len(c['touches'])
                placed = True
                break
        if not placed:
            clusters.append({'level': s['price'], 'touches': [s]})

    last_price = next((c for c in reversed(closes) if c), closes[-1])

    # ── punteggio misto: touch count + vicinanza al prezzo attuale ──
    scored = []
    for c in clusters:
        touch_count = len(c['touches'])
        dist_pct = abs(c['level'] - last_price) / last_price if last_price else 1
        proximity_score = max(0, 1 - dist_pct * 5)  # decade rapidamente oltre ~20% di distanza
        score = touch_count + proximity_score * 3    # peso 3x sulla vicinanza vs 1 tocco
        last_touch = max(c['touches'], key=lambda t: t['date'])
        scored.append({
            'price': round(c['level'], 4),
            'date': last_touch['date'],
            'touches': touch_count,
            '_score': score,
        })

    scored.sort(key=lambda s: s['_score'], reverse=True)
    top = scored[:top_n]
    for s in top:
        s.pop('_score')
    return sorted(top, key=lambda s: s['price'])

# ── MAIN ─────────────────────────────────────────────
def main():
    now = datetime.now()
    exec_type = get_execution_type()
    print(f"RAPTOR IWMO Fetch — {now.strftime('%Y-%m-%d %H:%M')} [{exec_type.upper()}]")

    # ── IWMO ETF (IWMO.MI) ────────────────────────────
    print("Scarico iShares World Momentum UCITS ETF (IWMO.MI)...")
    etp = yf.download("IWMO.MI", start="2014-01-01", interval="1d",
                       auto_adjust=True, progress=False)

    if hasattr(etp.columns, 'levels'):
        etp.columns = etp.columns.get_level_values(0)

    etp_closes  = [round(float(c),4) for c in etp['Close'].tolist()]
    etp_highs   = [round(float(c),4) for c in etp['High'].tolist()]
    etp_lows    = [round(float(c),4) for c in etp['Low'].tolist()]
    etp_volumes = [int(v) if v==v else 0 for v in etp['Volume'].tolist()]
    etp_dates   = [ts.strftime('%Y-%m-%d') for ts in etp.index]
    print(f"IWMO.MI: {len(etp_closes)} barre ({etp_dates[0]} → {etp_dates[-1]})")

    # ── BENCHMARK SWDA.MI (per forza relativa tab Regime) ─────
    print("Scarico iShares Core MSCI World UCITS ETF (SWDA.MI) come benchmark...")
    bench = yf.download("SWDA.MI", start="2014-01-01", interval="1d",
                         auto_adjust=True, progress=False)
    if hasattr(bench.columns, 'levels'):
        bench.columns = bench.columns.get_level_values(0)
    bench_closes = [round(float(c),4) for c in bench['Close'].tolist()]
    bench_dates  = [ts.strftime('%Y-%m-%d') for ts in bench.index]

    def calc_relative_strength(closes, dates, bench_closes, bench_dates):
        """Rendimento relativo IWMO/SWDA a 3M/6M/12M (~63/126/252 giorni di
        borsa), allineando le due serie per data."""
        bench_by_date = dict(zip(bench_dates, bench_closes))
        common_dates = [d for d in dates if d in bench_by_date]
        if not common_dates:
            return {}
        ratio = {d: c/bench_by_date[d] for d,c in zip(dates, closes) if d in bench_by_date}
        ordered = [ratio[d] for d in common_dates]
        last = ordered[-1]
        out = {'date': common_dates[-1]}
        for label, days in (('3m',63), ('6m',126), ('12m',252)):
            if len(ordered) > days and ordered[-days-1]:
                ret = (last - ordered[-days-1]) / ordered[-days-1] * 100
                out[f'ret_{label}'] = round(ret, 2)
            else:
                out[f'ret_{label}'] = None
        out['verdetto'] = ('SOVRAPERFORMA' if (out.get('ret_3m') or 0) > 0
                            else 'SOTTOPERFORMA')
        return out

    # ── ANALISI COMPLETA (MORNING + CLOSE) ─────────────
    if exec_type in ('morning', 'close', 'manual'):
        print(f"[{exec_type.upper()}] Calcolo analisi completa...")

        # Segnali RAPTOR
        def calc_signals_list(closes, kama_fast, kama_slow, volumes, ao_arr, er_arr):
            signals = []
            avg_vol = sum(volumes[-21:-1])/20 if len(volumes)>21 else 1
            for i in range(25, len(closes)):
                kf=kama_fast[i]; ks=kama_slow[i]
                if kf is None or ks is None:
                    signals.append(None); continue
                p=closes[i]
                if p>kf and kf>ks:   zona='LONG_CONF'
                elif p>kf and p>ks: zona='LONG_EARLY'
                elif p<ks:           zona='STOP' if (ks-p)/ks*100>2 else 'USCITA'
                else:                zona='GRIGIA'
                vr=volumes[i]/avg_vol if avg_vol>0 else 1
                gap_ok=ks>0 and abs(kf-ks)/ks>=0.003
                ao=ao_arr[i] if i<len(ao_arr) else 0
                sig=None
                baff=0
                for j in range(max(0,i-5),i+1):
                    if kama_fast[j] and closes[j]>kama_fast[j]: baff+=1
                    else: baff=0
                if zona=='LONG_CONF' and ao>0 and vr>=1.2 and baff>=3 and er_arr[i]>=0.35 and gap_ok:
                    sig='BUY3'
                elif zona=='LONG_EARLY' and ao>0 and vr>=1.1 and baff>=2 and er_arr[i]>=0.35:
                    sig='BUY2'
                elif zona in ('STOP','USCITA'): sig='SELL'
                signals.append(sig)
            return [None]*25 + signals

        # Stagionalità
        stagionalita = calc_stagionalita(etp_closes, etp_dates)

        # Forza relativa vs benchmark (sostituisce Antonacci)
        rel_strength = calc_relative_strength(etp_closes, etp_dates, bench_closes, bench_dates)

        # Indicatori RAPTOR su ETF
        etp_kama_fast = calc_kama(etp_closes, n=5,  fast=3, slow=20)
        etp_kama_slow = calc_kama(etp_closes, n=20, fast=2, slow=40)
        etp_rsi14     = calc_rsi(etp_closes, 14)
        etp_rsi5      = calc_rsi(etp_closes, 5)
        etp_ao        = calc_ao(etp_highs, etp_lows)
        etp_sar       = calc_sar(etp_highs, etp_lows)
        etp_er        = calc_er(etp_closes, 10)
        etp_signals   = calc_signals_list(etp_closes, etp_kama_fast, etp_kama_slow, etp_volumes, etp_ao, etp_er)

        etp_supports = find_supports(etp_closes, etp_dates)

        def fmt(arr):
            return [round(v,4) if v is not None else None for v in arr]

        output = sanitize({
            'execution_type': exec_type,
            'updated_at': now.isoformat(),
            'updated_display': now.strftime('%d/%m/%Y %H:%M'),

            'stagionalita': stagionalita,
            'rel_strength': rel_strength,

            'etp': {
                'dates':     etp_dates,
                'closes':    etp_closes,
                'highs':     etp_highs,
                'lows':      etp_lows,
                'volumes':   etp_volumes,
                'kama_fast': fmt(etp_kama_fast),
                'kama_slow': fmt(etp_kama_slow),
                'rsi14':     fmt(etp_rsi14),
                'rsi5':      fmt(etp_rsi5),
                'ao':        fmt(etp_ao),
                'sar':       fmt(etp_sar),
                'er':        etp_er,
                'signals':   etp_signals,
            },

            'etp_supports':  etp_supports,
        })

    # ── ANALISI LEGGERA INTRADAY (16:45) ────────────────
    else:  # intraday
        print(f"[INTRADAY] Calcolo segnali veloci...")
        try:
            with open('iwmo.json','r',encoding='utf-8') as f:
                output = json.load(f)
        except:
            output = {}

        etp_kama_fast = calc_kama(etp_closes, n=5,  fast=3, slow=20)
        etp_kama_slow = calc_kama(etp_closes, n=20, fast=2, slow=40)
        etp_rsi14     = calc_rsi(etp_closes, 14)
        etp_rsi5      = calc_rsi(etp_closes, 5)
        etp_ao        = calc_ao(etp_highs, etp_lows)
        etp_sar       = calc_sar(etp_highs, etp_lows)
        etp_er        = calc_er(etp_closes, 10)

        def calc_signals_intraday(closes, kama_fast, kama_slow, volumes, ao_arr, er_arr):
            signals = []
            avg_vol = sum(volumes[-21:-1])/20 if len(volumes)>21 else 1
            for i in range(25, len(closes)):
                kf=kama_fast[i]; ks=kama_slow[i]
                if kf is None or ks is None:
                    signals.append(None); continue
                p=closes[i]
                if p>kf and kf>ks:   zona='LONG_CONF'
                elif p>kf and p>ks: zona='LONG_EARLY'
                elif p<ks:           zona='STOP' if (ks-p)/ks*100>2 else 'USCITA'
                else:                zona='GRIGIA'
                vr=volumes[i]/avg_vol if avg_vol>0 else 1
                gap_ok=ks>0 and abs(kf-ks)/ks>=0.003
                ao=ao_arr[i] if i<len(ao_arr) else 0
                sig=None
                baff=0
                for j in range(max(0,i-5),i+1):
                    if kama_fast[j] and closes[j]>kama_fast[j]: baff+=1
                    else: baff=0
                if zona=='LONG_CONF' and ao>0 and vr>=1.2 and baff>=3 and er_arr[i]>=0.35 and gap_ok:
                    sig='BUY3'
                elif zona=='LONG_EARLY' and ao>0 and vr>=1.1 and baff>=2 and er_arr[i]>=0.35:
                    sig='BUY2'
                elif zona in ('STOP','USCITA'): sig='SELL'
                signals.append(sig)
            return [None]*25 + signals

        etp_signals = calc_signals_intraday(etp_closes, etp_kama_fast, etp_kama_slow, etp_volumes, etp_ao, etp_er)

        def fmt(arr):
            return [round(v,4) if v is not None else None for v in arr]

        output['execution_type'] = exec_type
        output['updated_at'] = now.isoformat()
        output['updated_display'] = now.strftime('%d/%m/%Y %H:%M')

        output['etp']['dates'] = etp_dates
        output['etp']['closes'] = etp_closes
        output['etp']['highs'] = etp_highs
        output['etp']['lows'] = etp_lows
        output['etp']['volumes'] = etp_volumes
        output['etp']['kama_fast'] = fmt(etp_kama_fast)
        output['etp']['kama_slow'] = fmt(etp_kama_slow)
        output['etp']['rsi14'] = fmt(etp_rsi14)
        output['etp']['rsi5'] = fmt(etp_rsi5)
        output['etp']['ao'] = fmt(etp_ao)
        output['etp']['sar'] = fmt(etp_sar)
        output['etp']['er'] = etp_er
        output['etp']['signals'] = etp_signals

        output.pop('fut', None)
        output.pop('fut_supports', None)
        output.pop('antonacci_fut', None)
        output.pop('antonacci_latest', None)
        output.pop('antonacci_etp', None)
        output.pop('antonacci_etp_latest', None)

        output = sanitize(output)

    with open('iwmo.json','w',encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',',':'), allow_nan=False)
    print(f"✅ iwmo.json aggiornato [{exec_type}]")

if __name__ == '__main__':
    main()
