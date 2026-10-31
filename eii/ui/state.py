"""App context, caching and session helpers for the Streamlit UI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import streamlit as st

from eii.config import Settings, get_settings, setup_logging
from eii.db import ResearchRepository, init_db
from eii.export import WorkbookManager
from eii.models import ResearchReport, SecurityIdentity, Source
from eii.providers import ProviderRegistry, ProviderUnavailable, build_registry
from eii.scoring import DEFAULT_SCORECARD, ScorecardConfig
from eii.services import BuildResult, build_report, refresh_analysis

log = logging.getLogger("eii.ui")

REPORT_KEY = "report"
PRICES_KEY = "price_history"
WEIGHTS_SETTING = "scorecard_weights"


@dataclass
class AppContext:
    settings: Settings
    registry: ProviderRegistry
    repo: ResearchRepository
    workbook: WorkbookManager


def get_app() -> AppContext:
    # Keyed on the exception class identity: when Streamlit hot-reloads code after a redeploy,
    # a context cached by the old modules would raise exception classes the new code can't catch.
    return _app_context(id(ProviderUnavailable))


@st.cache_resource(show_spinner=False)
def _app_context(code_version: int) -> AppContext:
    settings = get_settings()
    setup_logging(settings)
    init_db(settings.db_path)
    ctx = AppContext(settings=settings, registry=build_registry(settings),
                     repo=ResearchRepository(settings.db_path),
                     workbook=WorkbookManager(settings.excel_path, settings.backup_dir, settings.max_backups))
    ctx.workbook.ensure_workbook()
    log.info("Application context initialised")
    return ctx


def scorecard_config() -> ScorecardConfig:
    weights = get_app().repo.get_setting(WEIGHTS_SETTING, {}) or {}
    cfg = DEFAULT_SCORECARD.with_weights(weights)
    get_app().workbook.config = cfg
    return cfg


@dataclass
class Masters:
    nse: Optional[pd.DataFrame]
    bse: Optional[pd.DataFrame]
    sources: list[Source]
    messages: list[str]


@st.cache_data(ttl=3600, show_spinner="Loading exchange security lists…")
def load_masters() -> Masters:
    reg = get_app().registry
    nse = bse = None
    sources, messages = [], []
    try:
        nse = reg.nse.load()
        sources.append(reg.nse.source())
        if getattr(reg.nse, "last_warning", None):
            messages.append(reg.nse.last_warning)
    except ProviderUnavailable as exc:
        messages.append(f"NSE list unavailable: {exc}")
    try:
        bse = reg.bse.load()
        sources.append(reg.bse.source())
    except ProviderUnavailable as exc:
        messages.append(f"BSE codes unavailable — {exc}")
    return Masters(nse, bse, sources, messages)


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch(identity_json: str, weights_key: str) -> BuildResult:
    m = load_masters()
    identity = SecurityIdentity.model_validate_json(identity_json)
    return build_report(identity, get_app().registry, m.nse, m.bse, m.sources, scorecard_config())


def open_company(identity: SecurityIdentity, force_refresh: bool = False) -> BuildResult:
    if force_refresh:
        _fetch.clear()
    weights_key = str(sorted(scorecard_config().weights().items()))
    result = _fetch(identity.model_dump_json(), weights_key)
    previous: Optional[ResearchReport] = st.session_state.get(REPORT_KEY)
    report = result.report
    if previous is not None and previous.identity.key == identity.key:
        report = carry_analyst_inputs(previous, report)
    st.session_state[REPORT_KEY] = refresh_analysis(report, scorecard_config())
    st.session_state[PRICES_KEY] = result.price_history
    return result


def carry_analyst_inputs(old: ResearchReport, new: ResearchReport) -> ResearchReport:
    """Keep the analyst's work when provider data is refreshed."""
    return new.model_copy(update={
        "overview_notes": old.overview_notes, "business_model": old.business_model,
        "competitive_advantages": old.competitive_advantages, "governance": old.governance,
        "risks": old.risks, "scenarios": old.scenarios, "analyst": old.analyst,
        "sources": new.sources + [s for s in old.sources if s.kind.value == "user_supplied"
                                  and s.source_id not in {x.source_id for x in new.sources}],
    })


def current_report() -> Optional[ResearchReport]:
    return st.session_state.get(REPORT_KEY)


def update_report(**changes) -> ResearchReport:
    report = current_report().model_copy(update=changes)
    report = refresh_analysis(report, scorecard_config())
    st.session_state[REPORT_KEY] = report
    return report
