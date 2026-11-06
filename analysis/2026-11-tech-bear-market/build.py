"""Re-run the models and rebuild report.html from report_template.html."""
import json
import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
subprocess.run([sys.executable, str(here / "model.py")], check=True, stdout=subprocess.DEVNULL)
data = json.loads((here / "results.json").read_text())
tpl = (here / "report_template.html").read_text()
(here / "report.html").write_text(tpl.replace("/*__DATA__*/null", json.dumps(data)))
print("wrote report.html")
