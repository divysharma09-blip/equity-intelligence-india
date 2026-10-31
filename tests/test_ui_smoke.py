"""Offline smoke test: every page renders, and the save dialog's Yes/No paths behave.

Runs Streamlit's AppTest in a subprocess so cached settings/resources from other
tests cannot leak in. Uses the fictional NSE fixture list and no network calls.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import NSE_CSV

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/tests")
from streamlit.testing.v1 import AppTest
from conftest import FIN_DATA, ALPHA_ISIN
from datetime import datetime, timezone
from eii.models import *
from eii.services import refresh_analysis

out = {}
def run(at, name):
    at.run()
    out[name] = [e.value for e in at.exception]
    return at

at = AppTest.from_file(sys.argv[1] + "/app.py", default_timeout=60)
run(at, "search")
at.text_input[0].input("alpha"); at.button[0].click(); run(at, "search_query")
out["search_results"] = [s.value for s in at.subheader]

src = Source(source_id="s1", title="fixture", provider="pytest", kind=SourceKind.USER_SUPPLIED)
facts = [FinancialFact(metric=m, period_end=d, value=float(v), source_id="s1") for d, row in FIN_DATA.items() for m, v in row.items()]
ident = SecurityIdentity(company_name="Alpha Industries Limited", nse_symbol="ALPHAIND", isin=ALPHA_ISIN)
report = refresh_analysis(ResearchReport(identity=ident, financials=facts, sources=[src],
    market=MarketSnapshot(price=319.44, market_cap=3194.4, shares_outstanding=10, currency="INR",
                          as_of=datetime.now(timezone.utc)),
    verification=VerificationResult(status=VerificationStatus.VERIFIED)))
at.session_state["report"] = report
at.session_state["price_history"] = None
at.switch_page("eii/ui/views/workspace.py"); run(at, "workspace")
out["four_investor_hint"] = any("python -m eii.four_investor ALPHAIND" in c.value for c in at.code)
from eii.config import get_settings
from eii.four_investor.research import ClaudeAssessment, ParamAssessment, ResearchRun, save_run
save_run(get_settings().data_dir, ResearchRun(
    identity_key=ident.key, company_name=ident.company_name, nse_symbol="ALPHAIND",
    generated_at=datetime.now(timezone.utc), model="test", horizon="3–5 years", price_used=319.44,
    assessment=ClaudeAssessment(assessments=[ParamAssessment(param_id="buffett.moat.brand", rating=3)],
                                verdict="WATCHLIST", report_markdown="## Section 1: Executive summary")))
run(at, "workspace_with_run")
out["four_investor_verdict"] = any("WATCHLIST" in m.value for m in at.markdown)
next(b for b in at.button if b.label.startswith("Complete & save")).click(); run(at, "dialog")
next(b for b in at.button if b.label == "No, don't save").click(); run(at, "no")
out["after_no"] = [i.value for i in at.info if "Not saved" in i.value]
from eii.db import ResearchRepository
from eii.config import get_settings
repo = ResearchRepository(get_settings().db_path)
out["companies_after_no"] = len(repo.list_companies())
next(b for b in at.button if b.label.startswith("Complete & save")).click(); run(at, "dialog2")
next(b for b in at.button if b.label == "Yes, save").click(); run(at, "yes")
out["after_yes"] = [s.value for s in at.success if "Saved as version" in s.value]
out["companies_after_yes"] = len(repo.list_companies())
for page in ("saved", "compare", "settings"):
    at.switch_page(f"eii/ui/views/{page}.py"); run(at, page)
print("RESULT" + json.dumps(out))
"""


def test_all_pages_render_and_save_dialog(tmp_path):
    data = tmp_path / "data"
    (data / "reference").mkdir(parents=True)
    (data / "reference" / "nse_equity_list.csv").write_text(NSE_CSV)  # fresh cache -> no download
    env = {**os.environ, "EII_DATA_DIR": str(data), "EII_LOG_DIR": str(tmp_path / "logs"),
           "EII_ENABLE_YFINANCE": "false", "EII_ENABLE_NSE_FILINGS": "false", "EII_NSE_MASTER_URL": "https://example.invalid/none.csv"}
    proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], env=env, capture_output=True,
                          text=True, timeout=180, cwd=tmp_path)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, proc.stderr[-3000:]
    out = json.loads(line[len("RESULT"):])
    for page in ("search", "search_query", "workspace", "workspace_with_run", "dialog", "no", "yes", "saved", "compare", "settings"):
        assert out[page] == [], f"{page}: {out[page]}"
    assert out["search_results"] == ["2 candidate(s)"]
    assert out["four_investor_hint"] and out["four_investor_verdict"]
    assert out["after_no"] and out["companies_after_no"] == 0
    assert out["after_yes"] and out["companies_after_yes"] == 1
    assert (data / "Equity_Intelligence_India.xlsx").exists()
