# Tech sell-off buy plan (6 Nov 2026)

Analysis of the 25 tech stocks in bear-market territory after the 5 Nov 2026
sell-off, for a $1,000 low-risk, six-month buying plan.

| File | What it is |
|---|---|
| `model.py` | Inputs and the six models: screen, factor scorecard, reverse DCF, scenarios, Monte Carlo, bottom-signal checklist |
| `results.json` | Model output at the 5 Nov close (written by `model.py`) |
| `results_2026-11-10.json` | Same models re-run at the 10 Nov close (11 Nov update) |
| `results_2026-11-20.json` | Re-run at the 20 Nov close (23 Nov update; Nvidia excluded) |
| `report_template.html` | Report page; `build.py` injects `results.json` into it |
| `report.html` | Built report (published as an artifact) |
| `redteam.md` | Independent critique from a second AI model (Gemini) and what changed |
| `SOURCES.md` | Where each input came from |

Rebuild everything after editing inputs in `model.py`:

```
python3 analysis/2026-11-tech-bear-market/build.py
```

**Result:** buy Alphabet $250, TSMC $225, Nvidia $175, Broadcom $175 and Meta
$175 in three purchases (10–11 Nov, 18–20 Nov, 10–11 Dec) and keep a $200
reserve for a much deeper fall. Hold AMD without adding. Decide on Nebius
after its 12 Nov results.

Research only, not personal financial advice. Inputs are approximate and come
from news and data sites as of 5–6 Nov 2026.

**11 Nov update:** the rebound and hotter inflation cut the basket's 6-month
expected return from +11.5% to about +4%. Buy Alphabet ($62) now; wait for
Nvidia's 17 Nov results; buy TSMC, Broadcom and Meta only at or below their
buy zones ($360 / $310 / $570), with anything unbought deployed on 10–11 Dec.
AMD's trim rule triggered at $515: sell about a third.

**23 Nov update:** Nvidia dropped from the plan (CEO resigned; reported SEC
accounting inquiry). At the 20 Nov close the basket's 6-month expected return
is +14.5%. Buy TSMC $150, Meta $125 and Alphabet $100 now; Broadcom waits for
its 9 Dec results (unconfirmed Google TPU-cut report). Nebius sell rule
triggered on 12 Nov: exit on a rebound, by 11 Dec at the latest.
