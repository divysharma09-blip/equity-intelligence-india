import pandas as pd
import streamlit as st

from eii.config import FRAMEWORK_DISCLAIMER
from eii.db import backup_db
from eii.models import DATA_STATUS_LABELS
from eii.providers import ProviderUnavailable
from eii.providers.exchange_masters import BSEScripMaster
from eii.scoring import DEFAULT_SCORECARD
from eii.services.persistence import retry_excel_sync
from eii.ui.state import WEIGHTS_SETTING, get_app, load_masters, scorecard_config

st.title(":material/settings: Settings & methodology")
app = get_app()

# ------------------------------------------------------------------ providers
st.subheader("Data providers")
st.dataframe(pd.DataFrame([{"Provider": s.name, "Available": "✅" if s.available else "❌", "Status": s.message}
                           for s in app.registry.statuses()]), hide_index=True, width="stretch")
st.caption("No paid data services are used. Add providers by implementing `eii/providers/base.py` "
           "interfaces and registering them in `eii/providers/registry.py`.")
c1, c2 = st.columns(2)
if c1.button("Refresh NSE list now", icon=":material/refresh:"):
    try:
        app.registry.nse.refresh()
        load_masters.clear()
        st.success("NSE equity list downloaded.")
    except ProviderUnavailable as exc:
        st.error(str(exc))

with st.expander("BSE scrip list (needed for BSE codes)"):
    st.markdown(f"BSE blocks automated downloads. Download the list of scrips (Equity, Active) from "
                f"[{BSEScripMaster.download_page}]({BSEScripMaster.download_page}) and upload the CSV here.")
    up = st.file_uploader("BSE scrip CSV", type=["csv"])
    if up is not None:
        text = up.getvalue().decode("utf-8-sig", errors="replace")
        try:
            parsed = BSEScripMaster.parse(text)
        except ProviderUnavailable as exc:
            st.error(f"File not accepted: {exc}")
        else:
            target = app.settings.reference_dir / "bse_equity_list.csv"
            target.write_text(text, encoding="utf-8")
            load_masters.clear()
            st.success(f"Saved {len(parsed):,} active BSE scrips to {target.name}.")

# ------------------------------------------------------------------ scorecard weights
st.subheader("Scorecard weights")
st.caption(FRAMEWORK_DISCLAIMER)
cfg = scorecard_config()
with st.form("weights"):
    cols = st.columns(4)
    new_weights = {f.key: cols[i % 4].number_input(f.label, 0.0, 100.0, float(f.weight), 1.0, key=f"w_{f.key}")
                   for i, f in enumerate(cfg.factors)}
    total = sum(new_weights.values())
    b1, b2 = st.columns(2)
    if b1.form_submit_button("Save weights", type="primary"):
        if total <= 0:
            st.error("At least one weight must be positive.")
        else:
            app.repo.set_setting(WEIGHTS_SETTING, new_weights)
            st.success("Weights saved. Re-open or refresh a company to recompute its scores.")
    if b2.form_submit_button("Reset to defaults"):
        app.repo.set_setting(WEIGHTS_SETTING, DEFAULT_SCORECARD.weights())
        st.success("Defaults restored.")
st.caption("Weights are relative; they need not sum to 100. Factors without data are excluded and the rest "
           "re-normalised.")

# ------------------------------------------------------------------ storage
st.subheader("Research storage")
s = app.settings
c1, c2, c3 = st.columns(3)
c1.markdown(f"**Database**  \n`{s.db_path}`")
c2.markdown(f"**Workbook**  \n`{s.excel_path}`")
c3.markdown(f"**Backups**  \n`{s.backup_dir}`")
c1, c2, c3 = st.columns(3)
if s.excel_path.exists():
    c1.download_button("Download workbook", s.excel_path.read_bytes(), file_name=s.excel_path.name,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       icon=":material/download:")
pending = app.repo.unsynced_reports()
if c2.button(f"Retry Excel sync ({len(pending)} pending)", disabled=not pending, icon=":material/sync:"):
    synced, errors = retry_excel_sync(app.repo, app.workbook)
    (st.error if errors else st.success)(f"Synced {synced} report(s). " + " ".join(errors))
if c3.button("Back up database now", icon=":material/backup:"):
    path = backup_db(s.db_path, s.backup_dir, s.max_backups)
    st.success(f"Backed up to {path.name}")

# ------------------------------------------------------------------ methodology
st.subheader("Methodology")
for f in cfg.factors:
    with st.expander(f"{f.label} — weight {f.weight:g}"):
        st.write(f.description)
        st.dataframe(pd.DataFrame([{"Metric": m.label, "Rule": m.describe()} for m in f.metrics]),
                     hide_index=True, width="stretch")
st.markdown("""
- **Metric score**: linear between a *poor* (0) and *excellent* (100) threshold, clipped. Thresholds are judgement calls.
- **Missing data** is excluded, never scored as zero. **Coverage** shows how much of the framework had data.
- **Conclusion gate**: verified identity, ≥3 years of statements, ≥60% coverage, a current price and no critical
  warnings. Otherwise the app shows *“Insufficient verified data — no conclusion”*.
- **No buy/sell recommendations** are generated by this application.
- **Identity**: a name match is never sufficient. Verification needs a valid ISIN, an official exchange listing with
  matching symbol ↔ ISIN, and a matching data-provider name/ticker.
- **Scenarios**: EPS × (1+g)ⁿ × exit P/E, plus assumed dividend yield. All inputs are labelled *Assumption*.
- **Company types**: industrial, bank and NBFC metrics apply by type (type taken from the filing format). Insurers
  are not supported yet and never pass the gate.
- **Statements**: official NSE XBRL filings first (one basis for all years; standalone for banks), Yahoo as
  fallback and cross-check. EPS restated for splits/bonuses; inconsistent filings are excluded, not guessed.
- **Backtesting**: not implemented. When built it must use point-in-time data (publication dates) and a
  survivorship-free universe; see `eii/backtest.py`.
""")
st.markdown("**Data labels**")
st.dataframe(pd.DataFrame([{"Label": v, "Code": k.value} for k, v in DATA_STATUS_LABELS.items()]),
             hide_index=True, width="stretch")
st.caption(f"Logs: `{s.log_dir / 'eii.log'}`")
