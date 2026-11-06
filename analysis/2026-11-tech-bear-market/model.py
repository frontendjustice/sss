"""
Tech bear-market analysis, data as of the 5 Nov 2026 close.

Runs six models on the 25 stocks in the Kobeissi Letter list (5 Nov 2026):
  A. Hard screen          - removes names that fail the investor's constraints
  B. Factor scorecard     - weighted score for a low-risk, 6-month investor
  C. Reverse DCF          - EPS growth the price already assumes
  D. Scenario model       - probability-weighted 6-month return per stock
  E. Monte Carlo          - lump sum vs. staged buying for the $1,000
  F. Bottom-signal check  - how many classic "bottom" signals are flashing

All inputs are hand-entered from public sources (see SOURCES.md). Estimates
are approximate; change them in INPUTS and re-run: python3 model.py
"""
import json
import math
from pathlib import Path

import numpy as np

OUT = Path(__file__).parent

# --------------------------------------------------------------------------
# Inputs (5 Nov 2026 close unless noted)
# fwd_pe: next-12-month / next-fiscal-year P/E at the 5 Nov close
# growth: next-year EPS growth estimate (consensus or company outlook)
# quality / balance: 1-5 analyst ratings (moat+margins / balance sheet+FCF)
# cyclical: 1-5 (5 = deep commodity cycle), policy: 1-5 exposure to the
#           data-center / AI-regulation risk raised by the midterm result
# nov5: move on 5 Nov (a live read on how violently the stock reprices)
# --------------------------------------------------------------------------
INPUTS = {
    "NVDA": dict(name="Nvidia", price=181.60, dd=-25, fwd_pe=15.0, growth=0.45,
                 quality=5, balance=5, cyclical=3, policy=4, nov5=-9.3,
                 catalyst="Q3 FY27 earnings ~17 Nov (guided $108B revenue)"),
    "TSM": dict(name="TSMC", price=353.10, dd=-28, fwd_pe=16.5, growth=0.30,
                quality=5, balance=5, cyclical=2, policy=3, nov5=-10.8,
                catalyst="October sales 10 Nov; Q4 guided +58% y/y"),
    "AVGO": dict(name="Broadcom", price=291.53, dd=-41, fwd_pe=16.75, growth=0.40,
                 quality=5, balance=3, cyclical=2, policy=4, nov5=-10.4,
                 catalyst="FQ4 earnings 9 Dec (guided $21.7B AI revenue)"),
    "GOOGL": dict(name="Alphabet", price=282.57, dd=-32, fwd_pe=19.1, growth=0.15,
                  quality=5, balance=4, cyclical=1, policy=3, nov5=-2.2,
                  catalyst="Cloud +103%, $561B backlog; trades near 52-wk low $270.70"),
    "META": dict(name="Meta", price=605.08, dd=-26, fwd_pe=17.8, growth=0.09,
                 quality=5, balance=3, cyclical=1, policy=2, nov5=-4.0,
                 catalyst="Ad revenue +26%; 2027 EPS est. ~$34"),
    "AMD": dict(name="AMD", price=404.40, dd=-39, fwd_pe=24.5, growth=0.60,
                quality=4, balance=4, cyclical=3, policy=4, nov5=-17.3,
                catalyst="Q4 guide $17B; 2027 revenue growth >80% guided"),
    "MU": dict(name="Micron", price=656.39, dd=-48, fwd_pe=5.5, growth=0.05,
               quality=3, balance=4, cyclical=5, policy=4, nov5=-17.5,
               catalyst="Record FY26 ($133B rev) - classic peak-cycle setup"),
}
# META's 5 Nov move was not found in sources; -4% is an assumption between
# GOOGL (-2.2%) and the S&P 500 (-4.4%).
# Balance scores for GOOGL (5->4) and META (4->3) were cut after the red-team
# review: Alphabet's Q3 FCF was negative and S&P moved Meta's outlook to
# negative on capex-driven negative FCF in 2026-27.

# --------------------------------------------------------------------------
# A. Hard screen over the full 25-name list
# --------------------------------------------------------------------------
SCREEN = [
    # ticker, drawdown, verdict, reason
    ("SNDK", -66, "FAIL", "Deep NAND cycle; -22% on 5 Nov; peak-earnings risk"),
    ("ARM", -64, "FAIL", "Royalty miss on 4 Nov; still a premium multiple; 2nd-order AI play"),
    ("COIN", -63, "EXCLUDED", "Crypto-linked (your rule)"),
    ("NBIS", -62, "FAIL", "Loss-making, $16-25B capex, convertibles, 2 customers; earnings 12 Nov"),
    ("STX", -60, "FAIL", "Storage hardware cycle; high beta"),
    ("MRVL", -59, "FAIL", "-16% on 5 Nov; custom-silicon share risk vs AVGO"),
    ("NOW", -57, "FAIL", "Agentic AI threatens seat-based software pricing"),
    ("ORCL", -56, "FAIL", "$20B stock+bond raise, $125B+ debt, FCF negative to 2028, OpenAI concentration"),
    ("APP", -54, "FAIL", "Ad-tech, high volatility, governance/short-seller history"),
    ("CRCL", -54, "EXCLUDED", "Crypto-linked (your rule)"),
    ("SMCI", -51, "FAIL", "Accounting/governance history; thin server margins"),
    ("MU", -48, "SCORE", "Cheap (5-6x) but deep-cyclical - scored as a control"),
    ("DELL", -47, "FAIL", "-15% on 5 Nov; low-margin AI servers"),
    ("SPCX", -47, "FAIL", "New IPO: little trading history, very rich valuation"),
    ("QCOM", -43, "FAIL", "Handset revenue -26%, margin guide cut"),
    ("AVGO", -41, "SCORE", "Passes"),
    ("AMD", -39, "SCORE", "Passes (you already own it)"),
    ("HOOD", -38, "EXCLUDED", "Crypto-linked (your rule)"),
    ("GOOGL", -32, "SCORE", "Passes"),
    ("TSLA", -32, "FAIL", "Valuation rests on autonomy/robotics optionality; not low-risk"),
    ("PLTR", -28, "FAIL", "~95-120x forward earnings even after the drop"),
    ("TSM", -28, "SCORE", "Passes"),
    ("META", -26, "SCORE", "Passes"),
    ("CRWD", -26, "FAIL", "~150x+ forward earnings; software de-rating risk"),
    ("NVDA", -25, "SCORE", "Passes"),
]

# --------------------------------------------------------------------------
# B. Factor scorecard (weights tuned for a LOW-risk-tolerance investor)
# --------------------------------------------------------------------------
WEIGHTS = dict(valuation=0.25, growth=0.15, quality=0.20, balance=0.15, risk=0.20, catalyst=0.05)


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def scorecard(d):
    g = min(d["growth"], 0.60)  # cap so hyper-growth does not dominate
    peg = d["fwd_pe"] / max(g * 100, 1)
    # valuation: PEG 0.3 -> 100, PEG 2.0 -> 0, blended with absolute P/E
    val_peg = clamp((2.0 - peg) / 1.7 * 100)
    val_abs = clamp((35 - d["fwd_pe"]) / 30 * 100)
    # deep cyclicals look cheapest at the peak: penalise the "cheapness"
    val = (0.6 * val_peg + 0.4 * val_abs) * (1 - 0.12 * (d["cyclical"] - 1))
    growth = clamp(g / 0.60 * 100)
    quality = (d["quality"] - 1) / 4 * 100
    balance = (d["balance"] - 1) / 4 * 100
    # risk: daily shock size, cyclicality, policy exposure (higher = safer)
    shock = clamp(100 - abs(d["nov5"]) * 5)
    risk = 0.45 * shock + 0.30 * (5 - d["cyclical"]) / 4 * 100 + 0.25 * (5 - d["policy"]) / 4 * 100
    catalyst = 70  # all have a dated catalyst inside the 6-month window
    parts = dict(valuation=val, growth=growth, quality=quality, balance=balance, risk=risk, catalyst=catalyst)
    total = sum(parts[k] * w for k, w in WEIGHTS.items())
    return dict(peg=round(peg, 2), parts={k: round(v, 1) for k, v in parts.items()}, total=round(total, 1))


# --------------------------------------------------------------------------
# C. Reverse DCF: EPS CAGR (5y) needed to earn 10%/yr if the stock exits at
#    18x earnings (a mature large-cap tech multiple).
#    r = 10%: 10-yr Treasury ~4.85% + ~5% equity risk premium.
# --------------------------------------------------------------------------
R, EXIT_PE, YEARS = 0.10, 18.0, 5


def implied_growth(d):
    eps = d["price"] / d["fwd_pe"]
    needed = d["price"] * (1 + R) ** YEARS / (EXIT_PE * eps)
    return needed ** (1 / YEARS) - 1


# --------------------------------------------------------------------------
# D. 6-month scenario model
#    price_6m = forward EPS rolled half a year x scenario multiple
# --------------------------------------------------------------------------
SCENARIOS = {
    # name: (probability, multiple factor by beta bucket, EPS change by bucket)
    "Deep bear (recession / 2000-08 style)": (0.10, dict(hi=0.55, semi=0.60, plat=0.70), dict(hi=-0.30, semi=-0.20, plat=-0.12, cyc=-0.50)),
    "Bear (2022-style rate squeeze)": (0.25, dict(hi=0.75, semi=0.80, plat=0.88), dict(hi=-0.10, semi=-0.07, plat=-0.04, cyc=-0.30)),
    "Base (choppy repair)": (0.40, dict(hi=1.05, semi=1.05, plat=1.04), dict(hi=0.0, semi=0.0, plat=0.0, cyc=-0.10)),
    "Bull (V-shaped recovery)": (0.25, dict(hi=1.40, semi=1.30, plat=1.20), dict(hi=0.05, semi=0.03, plat=0.02, cyc=0.0)),
}
BUCKET = dict(NVDA="semi", TSM="semi", AVGO="semi", GOOGL="plat", META="plat", AMD="hi", MU="hi")


def scenario_returns(t, d):
    out = {}
    for sname, (p, mult, epsch) in SCENARIOS.items():
        b = BUCKET[t]
        eps_key = "cyc" if d["cyclical"] >= 5 else b
        eps_6m = (d["price"] / d["fwd_pe"]) * (1 + d["growth"]) ** 0.5 * (1 + epsch[eps_key])
        px = eps_6m * d["fwd_pe"] * mult[b]
        out[sname] = dict(p=p, ret=px / d["price"] - 1)
    ev = sum(v["p"] * v["ret"] for v in out.values())
    downside = sum(v["p"] * min(v["ret"], 0) for v in out.values())
    return out, ev, downside


# --------------------------------------------------------------------------
# E. Monte Carlo: $1,000 into the final basket - lump sum vs 3 tranches
# --------------------------------------------------------------------------
VOL = dict(NVDA=0.55, TSM=0.45, AVGO=0.50, GOOGL=0.35, META=0.40, AMD=0.65, MU=0.70)


def monte_carlo(weights, exp_ret, n=20000, days=126, seed=7):
    rng = np.random.default_rng(seed)
    tick = list(weights)
    k = len(tick)
    semis = {"NVDA", "TSM", "AVGO", "AMD", "MU"}
    corr = np.full((k, k), 0.45)
    for i, a in enumerate(tick):
        for j, b in enumerate(tick):
            if i == j:
                corr[i, j] = 1.0
            elif a in semis and b in semis:
                corr[i, j] = 0.75
    vol = np.array([VOL[t] for t in tick]) / math.sqrt(252)
    cov = np.outer(vol, vol) * corr
    L = np.linalg.cholesky(cov)
    # drift so that the 6-month mean matches the scenario expected return
    mu = np.array([math.log(1 + exp_ret[t]) / days for t in tick])
    w = np.array([weights[t] for t in tick])
    z = rng.standard_normal((n, days, k)) @ L.T
    logp = np.cumsum(z + mu - 0.5 * vol ** 2, axis=1)
    prices = np.exp(logp)  # relative to day 0 = 1

    # Lump sum: everything on day 0
    lump_val = 1000 * (prices[:, -1, :] @ w)
    lump_path = 1000 * (prices @ w)
    lump_min = lump_path.min(axis=1)

    # Plan: $250 day 0 (10-11 Nov), $300 day 6 (after NVDA earnings),
    # $250 day 23 (after 9 Dec FOMC/AVGO), $200 reserve that is only spent if
    # the basket falls 20% below the day-0 price (else it stays in cash).
    tranche_days, tranche_amt = [0, 6, 23], [250, 300, 250]
    shares = np.zeros((n, k))
    for td, amt in zip(tranche_days, tranche_amt):
        p_buy = prices[:, td, :] if td > 0 else np.ones((n, k))
        shares += (amt * w) / p_buy
    basket = prices @ w
    hit = basket <= 0.80
    first = np.where(hit.any(axis=1), hit.argmax(axis=1), -1)
    cash = np.where(first < 0, 200.0, 0.0)
    idx = np.where(first < 0, 0, first)
    p_res = prices[np.arange(n), idx, :]
    shares += np.where((first >= 0)[:, None], (200 * w) / p_res, 0.0)
    tr_val = (shares * prices[:, -1, :]).sum(axis=1) + cash
    reserve_used = float((first >= 0).mean())

    def stats(v):
        return dict(mean=float(v.mean()), median=float(np.median(v)), p5=float(np.percentile(v, 5)),
                    p95=float(np.percentile(v, 95)), prob_loss=float((v < 1000).mean()),
                    prob_loss_gt20=float((v < 800).mean()))
    res = dict(lump=stats(lump_val), tranche=stats(tr_val), reserve_used=reserve_used)
    res["lump"]["median_worst_point"] = float(np.median(lump_min))
    return res


# --------------------------------------------------------------------------
# F. Bottom-signal checklist (status at 6 Nov 2026 morning)
# --------------------------------------------------------------------------
SIGNALS = [
    ("Sentiment washed out (CNN Fear & Greed <= 20)", True, "Hit 20 'Extreme Fear' on 5 Nov"),
    ("Volatility spike (VIX > 28)", True, "VIX +70% to 28.2 - biggest jump since Apr 2025"),
    ("Capitulation-style breadth", True, "Nasdaq decliners 4,166 vs 855 advancers; SOX -12.3%, 2nd-worst day ever"),
    ("Fundamentals still rising", True, "AMD beat+raise, Nvidia guided $108B, 2027 hyperscaler capex guided up"),
    ("Rates turning supportive", False, "Fed hiked in Sept; 65-83% odds of another hike on 9 Dec; 10y ~4.85%"),
    ("Selling has stopped", False, "Taiwan market hit limit-down on 6 Nov - selling still spreading"),
    ("Price confirmation (follow-through rally)", False, "Not yet: need a strong up day on rising volume 4-10 days after the low"),
    ("Policy clarity on AI / data centers", False, "New Congress sits Jan 2027; regulation path unknown"),
]


def main():
    rows = []
    for t, d in INPUTS.items():
        sc = scorecard(d)
        scen, ev, dn = scenario_returns(t, d)
        ig = implied_growth(d)
        rows.append(dict(ticker=t, name=d["name"], price=d["price"], dd=d["dd"], fwd_pe=d["fwd_pe"],
                         growth=d["growth"], nov5=d["nov5"], peg=sc["peg"], parts=sc["parts"], score=sc["total"],
                         implied_growth=ig, growth_cushion=d["growth"] - ig, ev6m=ev, downside6m=dn,
                         scenarios={k: round(v["ret"], 4) for k, v in scen.items()}, catalyst=d["catalyst"]))
    rows.sort(key=lambda r: r["score"], reverse=True)

    # Final basket: top 5 by score, excluding AMD (already held - adding more
    # would concentrate the portfolio further) and MU (cyclical control).
    picks = [r for r in rows if r["ticker"] not in ("AMD", "MU")][:5]
    # Weights: score x inverse volatility, normalised, rounded to $25
    raw = {r["ticker"]: r["score"] / VOL[r["ticker"]] for r in picks}
    s = sum(raw.values())
    dollars = {t: round(1000 * v / s / 25) * 25 for t, v in raw.items()}
    diff = 1000 - sum(dollars.values())
    top = max(dollars, key=dollars.get)
    dollars[top] += diff
    weights = {t: v / 1000 for t, v in dollars.items()}
    exp_ret = {r["ticker"]: r["ev6m"] for r in picks}
    mc = monte_carlo(weights, exp_ret)
    port_ev = sum(weights[t] * exp_ret[t] for t in weights)

    result = dict(asof="2026-11-05 close", rows=rows, picks=[r["ticker"] for r in picks], dollars=dollars,
                  portfolio_ev6m=port_ev, monte_carlo=mc,
                  screen=[dict(ticker=a, dd=b, verdict=c, reason=d) for a, b, c, d in SCREEN],
                  signals=[dict(signal=a, on=b, note=c) for a, b, c in SIGNALS],
                  signals_on=sum(1 for _, b, _ in SIGNALS if b), signals_total=len(SIGNALS),
                  assumptions=dict(discount_rate=R, exit_pe=EXIT_PE, weights=WEIGHTS,
                                   scenario_probs={k: v[0] for k, v in SCENARIOS.items()}))
    (OUT / "results.json").write_text(json.dumps(result, indent=2))

    # Console summary
    print(f"{'Tkr':6}{'Score':>7}{'PEG':>6}{'Impl g':>8}{'Est g':>7}{'EV6m':>8}{'Down':>8}  scenarios")
    for r in rows:
        sc = " ".join(f"{v*100:+.0f}%" for v in r["scenarios"].values())
        print(f"{r['ticker']:6}{r['score']:7.1f}{r['peg']:6.2f}{r['implied_growth']*100:7.1f}%{r['growth']*100:6.0f}%"
              f"{r['ev6m']*100:7.1f}%{r['downside6m']*100:7.1f}%  {sc}")
    print("\nParts:")
    for r in rows:
        print(r["ticker"], r["parts"])
    print("\nPicks:", dollars, f"portfolio EV 6m {port_ev*100:.1f}%")
    print(json.dumps(mc, indent=2))
    print(f"Bottom signals on: {result['signals_on']}/{result['signals_total']}")


if __name__ == "__main__":
    main()
