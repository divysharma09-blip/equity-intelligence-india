"""Equity Intelligence India — Streamlit entry point.

Run with:  streamlit run app.py
"""

import logging

import streamlit as st

from eii.config import APP_NAME, FRAMEWORK_DISCLAIMER
from eii.db import DatabaseError

st.set_page_config(page_title=APP_NAME, page_icon=":material/insights:", layout="wide",
                   initial_sidebar_state="expanded")

try:
    from eii.ui.state import current_report, get_app
    get_app()
except DatabaseError as exc:
    st.error(f"The research database could not be opened: {exc}")
    st.stop()
except Exception as exc:  # show something useful instead of a bare traceback
    logging.getLogger("eii").exception("Startup failed")
    st.error(f"{APP_NAME} failed to start: {exc}. See logs/eii.log for details.")
    st.stop()

pages = [
    st.Page("eii/ui/views/search.py", title="Search", icon=":material/search:", default=True),
    st.Page("eii/ui/views/workspace.py", title="Research workspace", icon=":material/science:"),
    st.Page("eii/ui/views/saved.py", title="Saved companies", icon=":material/bookmarks:"),
    st.Page("eii/ui/views/compare.py", title="Compare", icon=":material/compare_arrows:"),
    st.Page("eii/ui/views/settings.py", title="Settings & methodology", icon=":material/settings:"),
]
nav = st.navigation(pages)

with st.sidebar:
    st.markdown(f"### :material/insights: {APP_NAME}")
    report = current_report()
    if report is not None:
        st.caption(f"Open: **{report.identity.company_name}**")
    st.caption(FRAMEWORK_DISCLAIMER)

nav.run()
