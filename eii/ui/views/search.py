import pandas as pd
import streamlit as st

from eii.models import SecurityIdentity
from eii.services import classify_query, search_securities
from eii.ui.state import load_masters, open_company

st.title(":material/search: Company search")
st.caption("Search by company name, NSE symbol, BSE code (6 digits) or ISIN. Results are *candidates*: "
           "the company's identity is verified against exchange data when you open it.")

masters = load_masters()
for msg in masters.messages:
    st.info(msg, icon=":material/info:")
if masters.nse is None and masters.bse is None:
    st.error("No exchange security list is available, so search cannot run. Check your internet "
             "connection, then use **Settings → Refresh NSE list**.", icon=":material/cloud_off:")
    st.stop()

with st.form("search", border=False):
    c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
    query = c1.text_input("Company name, NSE symbol, BSE code or ISIN",
                          placeholder="e.g. Infosys, INFY, 500209, INE009A01021",
                          value=st.session_state.get("last_query", ""))
    submitted = c2.form_submit_button("Search", type="primary", width="stretch")

if submitted:
    st.session_state["last_query"] = query
    st.session_state["search_results"] = search_securities(query, masters.nse, masters.bse)

results: list[SecurityIdentity] = st.session_state.get("search_results", [])
query = st.session_state.get("last_query", "")

if query and not results:
    qtype = classify_query(query)
    if qtype == "bse_code" and masters.bse is None:
        st.warning("BSE-code search needs the BSE scrip list. Add it in **Settings → BSE scrip list**, "
                   "or search by name, NSE symbol or ISIN.", icon=":material/warning:")
    else:
        st.warning(f"No listed equity matched “{query}”. Check spelling, or try the NSE symbol or ISIN.",
                   icon=":material/search_off:")

if results:
    st.subheader(f"{len(results)} candidate(s)")
    match_label = {"isin": "ISIN (exact)", "nse_symbol": "NSE symbol (exact)", "bse_code": "BSE code (exact)",
                   "nse_symbol_prefix": "NSE symbol (prefix)", "name": "Name contains",
                   "name_fuzzy": "Similar name"}
    df = pd.DataFrame([{
        "Company": r.company_name, "NSE symbol": r.nse_symbol or "—", "BSE code": r.bse_code or "—",
        "ISIN": r.isin or "—", "Series": r.series or "—",
        "Listed since": r.listing_date.isoformat() if r.listing_date else "—",
        "Matched on": match_label.get(r.matched_on, r.matched_on)} for r in results])
    if any(r.matched_on in ("name", "name_fuzzy") for r in results):
        st.caption(":material/warning: A name match does not prove identity — several companies can share "
                   "similar names. Confirm the symbol and ISIN before opening.")
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key="results_table")
    selected = event.selection.rows if event and event.selection else []
    if selected:
        chosen = results[selected[0]]
        st.markdown(f"**Selected:** {chosen.company_name} · NSE `{chosen.nse_symbol or '—'}` · "
                    f"ISIN `{chosen.isin or '—'}`")
        if st.button("Open in research workspace", type="primary", icon=":material/arrow_forward:"):
            with st.spinner("Verifying identity and retrieving available data…"):
                result = open_company(chosen)
            for err in result.provider_errors:
                st.warning(err)
            st.switch_page("eii/ui/views/workspace.py")
    else:
        st.caption("Select a row to open it.")
