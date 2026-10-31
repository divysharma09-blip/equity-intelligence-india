"""'Four investors' tab: Buffett / Munger / Dalio / Fisher scorecards and the saved Claude Code report."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from pydantic import ValidationError

from eii.calculations import facts_to_frame
from eii.calculations.financials import KeyMetrics
from eii.four_investor import (INVESTORS, PARAMS, RATING_LABELS, app_ratings, combined_score, load_run, merge,
                               score_band, score_investor, sector_ratings)
from eii.four_investor.evidence import Evidence, collect_with_registry
from eii.four_investor.framework import MIN_COVERAGE
from eii.models import FinancialFact, ResearchReport, SecurityIdentity
from eii.ui.components import fmt_inr, fmt_pct
from eii.ui.state import get_app

SOURCE_LABELS = {"app": "App (filed data & NSE evidence)", "model": "App DCF model",
                 "claude": "Claude Code research", "sector": "Sector profile"}


@st.cache_data(ttl=6 * 3600, show_spinner="Collecting public evidence from NSE (BRSR, announcements, "
                                          "shareholding, price history)…")
def _evidence(identity_json: str, facts_json: list[str], current_pe) -> Evidence:
    app = get_app()
    return collect_with_registry(SecurityIdentity.model_validate_json(identity_json),
                                 [FinancialFact.model_validate_json(f) for f in facts_json], current_pe,
                                 app.registry, app.settings.reference_dir)
SEVERITY_ICON = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "⚪"}


def _score_text(score, coverage) -> str:
    if score is None:
        return "—"
    return f"{score:.0f} / 100" + (" (provisional)" if coverage < MIN_COVERAGE else "")


def render(report: ResearchReport, km: KeyMetrics, data_dir) -> None:
    ident = report.identity
    st.caption("Four independent lenses on the same company. Scores measure alignment with each investor's "
               "philosophy, not the probability of a good return, and are not investment advice.")
    try:
        run = load_run(data_dir, ident.key)
    except (ValidationError, ValueError) as exc:
        run = None
        st.error(f"The saved Claude Code research for this company could not be read: {exc}")

    price = report.market.price if report.market else None
    ev = _evidence(ident.model_dump_json(), [f.model_dump_json() for f in report.financials], km.values.get("pe"))
    fixed = app_ratings(km, report.company_type, facts_to_frame(report.financials), price, ev)
    sector_name, by_sector = sector_ratings(ev.sector, report.company_type, ev, facts_to_frame(report.financials))
    ratings = merge(fixed, run.claude_ratings() if run else {}, by_sector)
    scores = [score_investor(i, ratings) for i in INVESTORS]
    combined = combined_score(scores)

    cols = st.columns(5)
    for col, s in zip(cols, scores):
        col.metric(INVESTORS[s.investor], _score_text(s.score, s.coverage),
                   help=f"{score_band(s.score)} · {s.coverage:.0%} of the {s.applicable_weight} applicable "
                        "points rated")
        col.caption(f"{score_band(s.score)} · coverage {s.coverage:.0%}")
    cols[4].metric("Combined (equal weight)", "—" if combined is None else f"{combined:.0f} / 100",
                   help="Average of the four investor scores. Shown only when all four have a score.")
    cols[4].caption(score_band(combined))

    st.caption(f"Sector: **{sector_name or 'not identified'}**"
               + (f" ({ev.sector_source})" if sector_name else "")
               + f" · {sum(1 for r in fixed.values() if r.rating is not None)} parameters rated from data and "
                 "the DCF model, "
                 f"{sum(1 for r in by_sector.values() if r.rating is not None)} from the sector profile, "
                 f"{sum(1 for r in ratings.values() if not r.applicable)} not applicable.")
    if run is None:
        st.info("Ratings from data and the sector profile are shown. A Claude Code research run on your Mac "
                "adds company-specific evidence (moat, management, macro, innovation), the intrinsic-value "
                "range and the full report, and replaces the sector-profile ratings:")
        st.code(f"cd equity-intelligence-india\npython -m eii.four_investor {ident.nse_symbol or ident.isin}",
                language="bash")
        st.caption("It uses Claude Code on your Claude plan (no API key), takes about 10–30 minutes, and saves "
                   f"`data/four_investor/{ident.key}.json`. Commit and push that file to show it here.")
    else:
        _research_summary(report, run)

    fig = go.Figure(go.Bar(
        x=[s.score or 0 for s in scores], y=[INVESTORS[s.investor] for s in scores], orientation="h",
        text=[f"{s.score:.0f}  ({s.coverage:.0%} rated)" if s.score is not None else "not rated" for s in scores],
        textposition="outside",
        marker_color=["#2E6FBA" if s.score is not None and s.coverage >= MIN_COVERAGE else "#9DB8DA"
                      if s.score is not None else "#CCCCCC" for s in scores]))
    fig.update_layout(height=230, xaxis=dict(range=[0, 115], title="Score (0–100)"),
                      yaxis=dict(autorange="reversed"), margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, width="stretch")
    st.caption("Light bars are provisional (under 60% of applicable points rated). Unrated parameters are "
               "excluded, never scored as zero; not-applicable ones are left out of coverage. Precedence: app "
               "data, then Claude research, then the app's DCF model and the sector profile.")

    for s in scores:
        label = INVESTORS[s.investor]
        with st.expander(f"{label} — {_score_text(s.score, s.coverage)}, coverage {s.coverage:.0%}"):
            st.dataframe(pd.DataFrame([{
                "Category": c.category,
                "Points": f"{c.points:.1f} / {c.rated_weight}" if c.rated_weight else "not rated",
                "Applicable weight": c.applicable_weight,
            } for c in s.categories]), hide_index=True, width="stretch")
            rows = []
            for p in PARAMS:
                if p.investor != s.investor:
                    continue
                r = ratings.get(p.id)
                rated = r is not None and r.rating is not None
                na = r is not None and not r.applicable
                rows.append({
                    "Category": p.category, "Parameter": p.label, "Weight": p.weight,
                    "Rating": "Not applicable" if na else f"{r.rating} · {RATING_LABELS[r.rating]}" if rated
                              else "Unavailable",
                    "Points": round(r.rating / 4 * p.weight, 2) if rated else None,
                    "Observed": r.observed if r else "",
                    "Evidence": r.evidence if r else "",
                    "Source": SOURCE_LABELS[r.source] if rated or na else "",
                    "Confidence": (r.confidence or "") if r else ""})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_config={"Evidence": st.column_config.TextColumn(width="large")})

    with st.expander("Public evidence used by the app"):
        for src in ev.sources:
            st.markdown(f"- [{src.title}]({src.url})" if src.url else f"- {src.title}")
        for err in ev.errors:
            st.caption(f"Not available: {err}")
        if ev.notes.get("brsr"):
            st.caption(ev.notes["brsr"])

    if run is not None:
        with st.expander("Full research report (Claude Code)", icon=":material/description:"):
            st.markdown(run.assessment.report_markdown)
        if run.assessment.sources:
            with st.expander("Research sources"):
                for src in run.assessment.sources:
                    st.markdown(f"- [{src.title}]({src.url})" if src.url else f"- {src.title}")


def _research_summary(report: ResearchReport, run) -> None:
    a = run.assessment
    st.markdown(f"#### Claude Code verdict: **{a.verdict}**")
    st.caption(f"Research run {run.generated_at:%d %b %Y %H:%M} UTC · {run.model} · horizon {run.horizon} · "
               f"price used {fmt_inr(run.price_used)}")
    if not (report.readiness and report.readiness.can_conclude):
        st.warning("The app's own data-sufficiency gate has not passed for this company. Treat this verdict as "
                   "provisional research, and check the Sources & data quality tab.")
    if a.verdict_summary:
        st.write(a.verdict_summary)

    c = st.columns(5)
    fmt = lambda x: "—" if x is None else f"{x:.0f} / 100"  # noqa: E731
    c[0].metric("Business quality", fmt(a.business_quality_score))
    c[1].metric("Valuation attractiveness", fmt(a.valuation_attractiveness_score), help="Higher = cheaper.")
    c[2].metric("Risk", fmt(a.risk_score), help="0 = lowest risk, 100 = highest.")
    c[3].metric("Dalio: macro resilience", fmt(a.dalio_macro_resilience_score))
    c[4].metric("Dalio: current macro", fmt(a.dalio_current_macro_environment_score))

    iv = a.intrinsic_value_per_share
    price = report.market.price if report.market else run.price_used
    if any(x is not None for x in (iv.bear, iv.base, iv.bull)):
        mos = (iv.base - price) / iv.base if iv.base and price else None
        st.dataframe(pd.DataFrame([{
            "Bear value": fmt_inr(iv.bear), "Base value": fmt_inr(iv.base), "Bull value": fmt_inr(iv.bull),
            "Current price": fmt_inr(price), "Margin of safety (base)": fmt_pct(mos),
            "Preferred buy price": fmt_inr(iv.preferred_buy_price),
            "Overvalued above": fmt_inr(iv.overvaluation_threshold)}]), hide_index=True, width="stretch")
        if iv.method_note:
            st.caption(iv.method_note)

    if a.red_flags:
        st.markdown("**Red flags**")
        for f in sorted(a.red_flags, key=lambda f: ["Critical", "High", "Medium", "Low"].index(f.severity)):
            st.markdown(f"{SEVERITY_ICON[f.severity]} **{f.severity}** ({f.status}) — {f.issue}"
                        + (f" · _{f.source}_" if f.source else ""))
