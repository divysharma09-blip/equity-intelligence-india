"""Sidebar AI assistant panel (shown on every page)."""

from __future__ import annotations

import json
import logging

import streamlit as st

from eii.chat import ask, available
from eii.ui.components import fmt_cr, fmt_inr

log = logging.getLogger("eii.ui")
MESSAGES_KEY = "chat_messages"
KEY_METRICS = ("roe_avg_3y", "roce_avg_3y", "ebit_margin_avg_3y", "revenue_cagr", "eps_cagr", "debt_to_equity",
               "interest_coverage", "cash_conversion_3y", "pe", "pb", "ev_ebitda", "fcf_yield",
               "promoter_pledge_pct", "gnpa_pct", "nnpa_pct", "cet1_ratio")


def _four_investor_context(report, km) -> list[str]:
    from eii.calculations import facts_to_frame
    from eii.four_investor import (INVESTORS, app_ratings, combined_score, load_run, merge, score_investor,
                                   sector_ratings)
    from eii.ui.four_investor_tab import _evidence
    from eii.ui.state import get_app

    fin = facts_to_frame(report.financials)
    ev = _evidence(report.identity.model_dump_json(), [f.model_dump_json() for f in report.financials],
                   km.values.get("pe"))
    app = app_ratings(km, report.company_type, fin, report.market.price if report.market else None, ev)
    sector, by_sector = sector_ratings(ev.sector, report.company_type, ev, fin)
    run = load_run(get_app().settings.data_dir, report.identity.key)
    scores = [score_investor(i, merge(app, run.claude_ratings() if run else {}, by_sector)) for i in INVESTORS]
    lines = [f"Sector: {sector or 'unknown'}",
             "Four-investor scores: " + ", ".join(
                 f"{INVESTORS[s.investor]} {s.score:.0f}/100 ({s.coverage:.0%} coverage)" if s.score is not None
                 else f"{INVESTORS[s.investor]} not rated" for s in scores)
             + (f"; combined {c:.0f}/100" if (c := combined_score(scores)) is not None else "")]
    if run is not None:
        a = run.assessment
        iv = a.intrinsic_value_per_share
        lines.append(f"Claude Code research run ({run.generated_at:%d %b %Y}): verdict {a.verdict}. "
                     f"{a.verdict_summary}")
        if iv.base is not None:
            lines.append(f"Intrinsic value per share: bear {iv.bear}, base {iv.base}, bull {iv.bull}; "
                         f"preferred buy price {iv.preferred_buy_price}")
        lines += [f"Red flag ({f.severity}, {f.status}): {f.issue}" for f in a.red_flags[:8]]
    return lines


def build_context() -> str:
    """The open company's data as plain text for the assistant."""
    from eii.services import compute_key_metrics
    from eii.ui.state import current_report

    report = current_report()
    if report is None:
        return ""
    ident, km = report.identity, compute_key_metrics(report)
    m = report.market
    lines = [f"Open company: {ident.company_name} (NSE {ident.nse_symbol or '—'}, ISIN {ident.isin or '—'}), "
             f"type {report.company_type}, statements {report.statement_basis or 'n/a'}.",
             f"Identity verification: {report.verification.status.value if report.verification else 'n/a'}."]
    if m and m.price:
        lines.append(f"Price {fmt_inr(m.price)} as of {m.as_of:%d %b %Y}; market cap "
                     f"{fmt_cr(km.values.get('market_cap'))}.")
    metrics = {k: round(v, 4) for k in KEY_METRICS if (v := km.values.get(k)) is not None}
    lines.append(f"Key metrics (ratios as fractions): {json.dumps(metrics)}")
    if report.scorecard:
        sc = report.scorecard
        lines.append(f"App framework score: {sc.overall_score}, coverage {sc.coverage:.0%}; factors: "
                     + ", ".join(f"{f.label} {f.score:.0f}" for f in sc.factors if f.score is not None))
    if report.readiness:
        lines.append("Data sufficiency gate: " + ("passed" if report.readiness.can_conclude else
                                                  "not passed — " + "; ".join(report.readiness.reasons)))
    lines += [f"Warning [{w.severity}] {w.section}: {w.message}" for w in report.warnings
              if w.severity != "info"][:10]
    try:
        lines += _four_investor_context(report, km)
    except Exception as exc:  # context is best-effort; never block a question
        log.warning("Four-investor context unavailable: %s", exc)
    return "\n".join(lines)


def render() -> None:
    with st.sidebar:
        st.divider()
        head, clear = st.columns([3, 1], vertical_alignment="center")
        head.markdown("#### :material/smart_toy: AI assistant")
        ok, detail = available()
        if not ok:
            st.caption(detail)
            return
        messages: list[dict] = st.session_state.setdefault(MESSAGES_KEY, [])
        if clear.button("Clear", key="chat_clear", disabled=not messages, help="Start a new conversation"):
            messages.clear()
            st.rerun()
        st.caption("Ask about markets, any company, or this app. Uses Claude Code on your Mac and can search "
                   "the web. Not investment advice.")
        box = st.container(height=440 if messages else 90)
        with box:
            if not messages:
                st.caption("e.g. *Why did this company score low for Munger?* · *What did RBI do at its last "
                           "policy meeting?* · *How is the margin of safety calculated?*")
            for msg in messages:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
        question = st.chat_input("Ask anything…", key="chat_input")
        if not question:
            return
        messages.append({"role": "user", "content": question})
        with box:
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                status = st.empty()
                status.caption("Thinking…")
                errors: list[str] = []

                def stream():
                    for kind, payload in ask(question, messages[:-1], build_context()):
                        if kind == "text":
                            status.empty()
                            yield payload
                        elif kind == "status":
                            status.caption(payload)
                        else:
                            errors.append(payload)

                answer = st.write_stream(stream())
                status.empty()
                if errors:
                    st.error(errors[-1])
        text = answer if isinstance(answer, str) else "".join(map(str, answer or []))
        messages.append({"role": "assistant", "content": text or f"⚠️ {errors[-1] if errors else 'No answer.'}"})
