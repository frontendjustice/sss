# Tech sell-off buy plan (6 Nov 2026)

Analysis of the 25 tech stocks in bear-market territory after the 5 Nov 2026
sell-off, for a $1,000 low-risk, six-month buying plan.

| File | What it is |
|---|---|
| `model.py` | Inputs and the six models: screen, factor scorecard, reverse DCF, scenarios, Monte Carlo, bottom-signal checklist |
| `results.json` | Model output (written by `model.py`) |
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
