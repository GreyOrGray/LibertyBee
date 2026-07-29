"""site_metrics.py — recompute every aggregate published on libertybee.org from a frozen corpus.

Portable: point --corpus at ANY restored copy of the baseline .bak (whatever you named it).
Prints a verification report (each site figure recomputed vs its published value) and can
also write it to a file and/or emit machine-readable JSON for diffing / CI.

  python site_metrics.py --corpus LibertyBee_V03R4_Baseline
  python site_metrics.py --corpus MyRestoredCopy --out report.txt --json metrics.json
  python site_metrics.py --corpus MyRestoredCopy --server localhost\\SQLEXPRESS
"""
import argparse, json, pyodbc, statistics as st
from collections import defaultdict

# shipped below-market tenure ladder: +5% at 36mo, +5% at 72mo, +10% at 120mo (cumulative)
TIERS = [(36, 0.05), (72, 0.05), (120, 0.10)]
def reduction(t):
    tot = 0.0
    for m, p in TIERS:
        if t >= m: tot += p
        else: break
    return tot
_pwc = {}
def paid_weight(dur):
    if dur not in _pwc:
        _pwc[dur] = sum(1.0 - reduction(t) for t in range(dur))
    return _pwc[dur]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="database name of a restored baseline corpus")
    ap.add_argument("--server", default="localhost")
    ap.add_argument("--driver", default="ODBC Driver 17 for SQL Server")
    ap.add_argument("--out", default=None, help="write the text report to this file")
    ap.add_argument("--json", default=None, help="write machine-readable metrics to this JSON file")
    ap.add_argument("--quiet", action="store_true", help="suppress console output")
    a = ap.parse_args()

    lines, M = [], {}
    def emit(s=""):
        lines.append(s)
        if not a.quiet: print(s)

    cx = pyodbc.connect(f"DRIVER={{{a.driver}}};SERVER={a.server};DATABASE={a.corpus};Trusted_Connection=yes", timeout=180)
    cu = cx.cursor()

    survived, hhc, pcnt = {}, {}, {}
    for rg, sd, s, hh, pp in cu.execute("SELECT Rung,Seed,Survived,HouseholdCount,PersonCount FROM v1.run_summary"):
        run = (float(rg), sd); survived[run] = bool(s); hhc[run] = hh; pcnt[run] = pp
    sv = [r for r in survived if survived[r]]
    M["corpus"] = a.corpus; M["runs"] = len(survived); M["survived"] = len(sv)
    emit(f"corpus={a.corpus}  runs={len(survived)}  survived={len(sv)}\n")

    infl = defaultdict(dict); datemap = defaultdict(dict)
    for rg, sd, mi, rr, dt in cu.execute("SELECT Rung,Seed,MonthIndex,RentRate,InflationDate FROM v1.inflation_schedule ORDER BY Rung,Seed,MonthIndex"):
        run = (float(rg), sd); infl[run][int(mi)] = float(rr); datemap[run][(dt.year, dt.month)] = int(mi)
    prefix, horizon = {}, {}
    for run, rates in infl.items():
        fac = 1.0; ps = [0.0]
        for m in sorted(rates): fac *= (1.0 + rates[m]); ps.append(ps[-1] + fac)
        prefix[run] = ps; horizon[run] = len(ps) - 1
    base = {}
    for rg, sd, uid, br in cu.execute("SELECT Rung,Seed,UnitID,BaseRent FROM v1.property_units WHERE BaseRent IS NOT NULL"):
        base[(float(rg), sd, uid)] = float(br)
    term_mi = {}
    for rg, sd, lid, td in cu.execute("SELECT Rung,Seed,LeaseID,TerminationDate FROM v1.lease_termination WHERE TerminationDate IS NOT NULL"):
        run = (float(rg), sd); mi = datemap[run].get((td.year, td.month))
        if mi is not None: term_mi[(run, lid)] = mi

    hh_sav = defaultdict(float); run_sav = defaultdict(float); run_mkt = defaultdict(float)
    ten_completed, ten_censored = defaultdict(list), defaultdict(list); byyear = defaultdict(list)
    for rg, sd, lid, hid, uid, sdate, mrent in cu.execute(
            "SELECT Rung,Seed,LeaseID,HouseholdID,UnitID,LeaseStartDate,MonthlyRent FROM v1.lease"):
        run = (float(rg), sd); br = base.get((run[0], run[1], uid))
        if br is None or mrent is None: continue
        dm = datemap[run]; ps = prefix.get(run)
        if not ps: continue
        smi = dm.get((sdate.year, sdate.month))
        if smi is None: continue
        smi = max(1, smi); tm = term_mi.get((run, lid)); completed = tm is not None
        emi = min(tm if completed else horizon[run], horizon[run])
        if emi < smi: continue
        dur = emi - smi + 1
        market = br * (ps[emi] - ps[smi - 1]); paid = float(mrent) * paid_weight(dur)
        if survived.get(run):
            hh_sav[(run, hid)] += market - paid
            run_sav[run] += market - paid; run_mkt[run] += market
            (ten_completed if completed else ten_censored)[run].append(dur)
            mr = float(mrent)
            for Y in range(1, 21):
                mo = Y * 12
                if mo > dur: break
                e = smi + mo - 1
                byyear[Y].append(br * (ps[e] - ps[smi - 1]) - mr * paid_weight(mo))

    per_hh = list(hh_sav.values())
    tot_sav = sum(run_sav[r] for r in sv); tot_mkt = sum(run_mkt[r] for r in sv)
    tot_hh = sum(hhc[r] for r in sv); tot_pp = sum(pcnt[r] for r in sv)
    comp = [d for r in sv for d in ten_completed[r]]; cens = [d for r in sv for d in ten_censored[r]]
    allten = comp + cens
    M["below_market_pct"] = round(100*tot_sav/tot_mkt, 1)
    M["saved_per_hh_mean"] = round(tot_sav/tot_hh); M["saved_per_hh_median"] = round(st.median(per_hh))
    M["saved_per_person_mean"] = round(tot_sav/tot_pp)
    M["tenure_completed_median_yr"] = round(st.median(comp)/12, 2); M["tenure_censored_median_yr"] = round(st.median(allten)/12, 2)
    M["savings_curve"] = {f"yr{Y}": round(st.median(byyear[Y])) for Y in (5, 10, 20)}

    emit("== SAVINGS vs market (survived runs) ==")
    emit(f"  realized below-market: {M['below_market_pct']}%   [site: ~27%]")
    emit(f"  per household  MEAN  : ${M['saved_per_hh_mean']:,}        [site 'mean' ~$85k]")
    emit(f"  per household MEDIAN : ${M['saved_per_hh_median']:,}   (n={len(per_hh)} households)  [site headline '~$33k median']")
    emit(f"  per person     MEAN  : ${M['saved_per_person_mean']:,}")
    emit("== TENURE ==")
    emit(f"  completed         : median {M['tenure_completed_median_yr']}yr   [site ~3.1yr]")
    emit(f"  censored-inclusive: median {M['tenure_censored_median_yr']}yr   [site ~4yr]")
    emit("== SAVINGS BY TENURE YEAR (median per lease reaching year Y) ==")
    for Y in (5, 10, 20):
        emit(f"  yr{Y:>2}: ${M['savings_curve'][f'yr{Y}']:>10,}   [site ~$45k / $140k / $505k]")

    acc = red = forf = 0.0; hhq = 0
    for a_, r_, f_, h_ in cu.execute("SELECT TCSAccruedTotal,TCSRedeemedTotal,TCSForfeitedTotal,HouseholdCount FROM v1.run_summary WHERE Survived=1"):
        acc += float(a_ or 0); red += float(r_ or 0); forf += float(f_ or 0); hhq += h_
    M["tcs_redeemed_pct"] = round(100*red/acc, 1); M["tcs_forfeited_pct"] = round(100*forf/acc, 1)
    M["tcs_held_pct"] = round(100*(acc-red-forf)/acc, 1); M["tcs_redeemed_per_hh"] = round(red/hhq)
    emit("== TENANT CREDITS (survived) ==")
    emit(f"  redeemed {M['tcs_redeemed_pct']}% / forfeited {M['tcs_forfeited_pct']}% / held {M['tcs_held_pct']}%   [site 50.7/20.7/28.5]")
    emit(f"  redeemed per household: ${M['tcs_redeemed_per_hh']:,}   [site ~$7,596]")

    byr = defaultdict(list)
    for r in sv: byr[r[0]].append(hhc[r])
    hi = max(byr)
    # the site anchors "reach" at the $4.5M 100%-survival threshold, not the lowest surviving rung
    thr = min((rg for rg in byr if rg >= 4_500_000), default=min(byr))
    M["reach"] = {f"${rg/1e6:.1f}M": round(st.mean(v)) for rg, v in sorted(byr.items())}
    emit("== REACH (survived households per org) ==")
    emit(f"  ${thr/1e6:.1f}M (survival threshold) -> {round(st.mean(byr[thr]))} ... ${hi/1e6:.1f}M -> {round(st.mean(byr[hi]))}   [site ~61 -> ~163]")
    cx.close()

    if a.out:
        with open(a.out, "w") as f: f.write("\n".join(lines) + "\n")
        if not a.quiet: print(f"\n[report written to {a.out}]")
    if a.json:
        with open(a.json, "w") as f: json.dump(M, f, indent=2)
        if not a.quiet: print(f"[metrics written to {a.json}]")

if __name__ == "__main__":
    main()
