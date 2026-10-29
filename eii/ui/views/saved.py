import streamlit as st

from eii.ui.components import fmt_pct
from eii.ui.state import PRICES_KEY, REPORT_KEY, get_app, scorecard_config
from eii.services import refresh_analysis

st.title(":material/bookmarks: Saved companies")
repo = get_app().repo
companies = repo.list_companies()

if companies.empty:
    st.info("No saved research yet. Open a company from **Search**, complete the workspace, then choose "
            "**Complete & save report…**.", icon=":material/inbox:")
    st.stop()

view = companies.assign(
    score=companies["overall_score"].map(lambda x: "—" if x != x or x is None else f"{x:.0f}"),
    coverage=companies["score_coverage"].map(lambda x: fmt_pct(x, 0) if x == x else "—"),
    gate=companies["can_conclude"].map({1: "Passed", 0: "Not passed"}),
    excel=companies["excel_synced"].map({1: "✅", 0: "⚠️ pending"}))
st.caption(f"{len(companies)} companies · stored in `{get_app().settings.db_path.name}` and the Excel workbook. "
           "Scores are shown for reference; only rows where the gate passed support any conclusion.")
event = st.dataframe(
    view[["company_name", "nse_symbol", "isin", "sector", "verification_status", "score", "coverage", "gate",
          "versions", "latest_saved_at", "excel"]],
    hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
    column_config={"company_name": "Company", "nse_symbol": "NSE", "isin": "ISIN", "sector": "Sector",
                   "verification_status": "Verification", "score": "Framework score", "coverage": "Coverage",
                   "gate": "Conclusion gate", "versions": "Versions", "latest_saved_at": "Last saved (UTC)",
                   "excel": "Excel"})

rows = event.selection.rows if event and event.selection else []
if not rows:
    st.caption("Select a company to see its report versions and history.")
    st.stop()

company = companies.iloc[rows[0]]
st.subheader(company["company_name"])
versions = repo.list_reports(int(company["company_id"]))
st.dataframe(versions.assign(
    overall_score=versions["overall_score"].round(1),
    score_coverage=versions["score_coverage"].map(lambda x: fmt_pct(x, 0) if x == x else "—")),
    hide_index=True, width="stretch",
    column_config={"report_id": "Report ID", "version": "Version", "status": "Status",
                   "verification_status": "Verification", "overall_score": "Score", "score_coverage": "Coverage",
                   "can_conclude": "Gate passed", "analyst": "Analyst", "excel_synced": "In Excel",
                   "excel_error": "Excel error", "created_at": "Saved (UTC)"})

c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
version_choice = c1.selectbox("Version", versions["version"].tolist(), format_func=lambda v: f"Version {v}")
if c2.button("Open in workspace", type="primary", icon=":material/open_in_new:", width="stretch"):
    rid = versions.loc[versions["version"] == version_choice, "report_id"].iloc[0]
    report = repo.get_report(rid)
    if report is None:
        st.error("Report not found in the database.")
    else:
        # Recompute derived values with current settings; stored facts and inputs are unchanged.
        st.session_state[REPORT_KEY] = refresh_analysis(report, scorecard_config())
        st.session_state[PRICES_KEY] = None
        st.switch_page("eii/ui/views/workspace.py")
st.caption("Opening a saved version lets you continue the research; saving again creates a new version. "
           "Older versions are never overwritten.")

with st.expander("Research history (audit trail)"):
    st.dataframe(repo.history(int(company["company_id"])), hide_index=True, width="stretch")
