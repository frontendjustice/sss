"""Re-run the models and rebuild report.html from report_template.html."""
import json
import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
subprocess.run([sys.executable, str(here / "model.py")], check=True, stdout=subprocess.DEVNULL)
data = json.loads((here / "results.json").read_text())
tpl = (here / "report_template.html").read_text()
upd = json.loads((here / "results_2026-11-10.json").read_text())
upd2 = json.loads((here / "results_2026-11-20.json").read_text())
html = tpl.replace("/*__DATA__*/null", json.dumps(data)).replace("/*__UPDATE__*/null", json.dumps(upd)).replace("/*__UPDATE2__*/null", json.dumps(upd2))
(here / "report.html").write_text(html)
print("wrote report.html")
