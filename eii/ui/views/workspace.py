from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from pydantic import ValidationError

from eii.calculations import compute_ratios, facts_to_frame
from eii.calculations.financials import METRIC_LABELS, RATIO_LABELS, financial_currency
from eii.db import DatabaseError
from eii.models import (DATA_STATUS_LABELS, HORIZONS, GovernanceInputs, Risk, ScenarioAssumption,
                        Source, SourceKind)
from eii.calculations.financials import RATIO_METRICS
from eii.scoring import COMPANY_TYPE_LABELS, score_band
from eii.services import compute_key_metrics
from eii.services.persistence import save_completed_report
from eii.ui.components import (disclaimer, fmt_cr, fmt_inr, fmt_num, fmt_pct, fmt_x, fmt_by_unit,
                               no_report_message, readiness_banner, status_legend, verification_badge)
from eii.ui.state import PRICES_KEY, current_report, get_app, open_company, scorecard_config, update_report
from eii.valuation import dcf_value, reverse_dcf_implied_growth, suggest_scenarios

report = current_report()
if report is None:
    st.title(":material/science: Research workspace")
    no_report_message()
    st.stop()

ident = report.identity
rid = report.report_id
km = compute_key_metrics(report)
kv = km.values
fin_ccy = financial_currency(report.financials) or "INR"
scale, unit_label = (1e7, "₹ crore") if fin_ccy == "INR" else (1e6, f"{fin_ccy} million")

# ------------------------------------------------------------------ header
h1, h2 = st.columns([4, 1], vertical_alignment="center")
with h1:
    st.title(ident.company_name)
    st.caption(" · ".join(x for x in [
        f"NSE **{ident.nse_symbol}**" if ident.nse_symbol else "NSE —",
        f"BSE **{ident.bse_code}**" if ident.bse_code else "BSE code not available",
        f"ISIN **{ident.isin}**" if ident.isin else "ISIN —",
        f"Type: **{COMPANY_TYPE_LABELS.get(report.company_type, report.company_type)}**",
        f"Statements: **{report.statement_basis or 'n/a'}**",
        f"Status: **{report.status}**"]))
    verification_badge(report)
with h2:
    if st.button("Refresh data", icon=":material/refresh:", width="stretch",
                 help="Re-fetch provider data. Your notes, governance inputs, risks and scenarios are kept."):
        with st.spinner("Refreshing…"):
            open_company(ident, force_refresh=True)
        st.rerun()

flash = st.session_state.pop("save_msg", None)
if flash:
    getattr(st, flash[0])(flash[1])

readiness_banner(report.readiness)

tabs = st.tabs(["Overview", "Business & moat", "Financials", "Quality · Growth · Valuation · Strength",
                "Governance & risks", "Valuation scenarios", "Sources & data quality"])


def _src_caption(source_id: str | None) -> None:
    s = report.source_by_id(source_id)
    if s:
        st.caption(f"Source: [{s.title}]({s.url})" if s.url else f"Source: {s.title}")


def _text_section(label: str, field: str, help_text: str) -> None:
    key = f"{field}_{rid}"

    def _save():
        update_report(**{field: st.session_state[key]})

    st.text_area(label, value=getattr(report, field), key=key, on_change=_save, height=160, help=help_text)
    st.caption("Label: **Analyst-entered**. Saved with the report.")


# ------------------------------------------------------------------ 1. overview
with tabs[0]:
    m = report.market
    c = st.columns(4)
    c[0].metric("Price", fmt_inr(m.price if m else None))
    c[1].metric("Market cap", fmt_cr(kv.get("market_cap")))
    c[2].metric("52-week range", f"{fmt_inr(m.low_52w, 0)} – {fmt_inr(m.high_52w, 0)}" if m and m.high_52w else "—")
    c[3].metric("12-month return", fmt_pct(m.return_12m if m else None))
    if m:
        _src_caption(m.source_id)
        if m.as_of:
            st.caption(f"Market data retrieved {m.as_of:%d %b %Y %H:%M} UTC; quotes may be delayed.")
    else:
        st.info("No market data available from the configured providers.", icon=":material/cloud_off:")

    left, right = st.columns([3, 2])
    with left:
        prices = st.session_state.get(PRICES_KEY)
        if prices is not None and not prices.empty:
            px = prices["close"]
            fig = go.Figure()
            fig.add_scatter(x=px.index, y=px, name="Close (adjusted)", line=dict(width=2))
            fig.add_scatter(x=px.index, y=px.rolling(200).mean(), name="200-day average",
                            line=dict(width=1.5, dash="dot"))
            fig.update_layout(height=320, margin=dict(l=0, r=0, t=30, b=0), title="Price, last 2 years (₹)",
                              legend=dict(orientation="h", y=-0.15), hovermode="x unified")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Price history not loaded. Click **Refresh data** to fetch it.", icon=":material/show_chart:")
    with right:
        st.markdown("**Identity**")
        st.dataframe(pd.DataFrame({"Field": ["Company", "NSE symbol", "BSE code", "ISIN", "Series",
                                             "Listed on NSE since", "Face value"],
                                   "Value": [ident.company_name, ident.nse_symbol or "—", ident.bse_code or "—",
                                             ident.isin or "—", ident.series or "—",
                                             ident.listing_date.isoformat() if ident.listing_date else "—",
                                             fmt_inr(ident.face_value, 0)]}),
                     hide_index=True, width="stretch")
        p = report.profile
        if p:
            st.markdown(f"**Sector:** {p.sector or '—'}  \n**Industry:** {p.industry or '—'}  \n"
                        f"**Website:** {p.website or '—'}")
            _src_caption(p.source_id)
        own = report.ownership
        if own:
            st.markdown(f"**Ownership** (as filed, quarter ended {own.as_of:%d %b %Y})")
            oc = st.columns(3)
            oc[0].metric("Promoter holding", fmt_pct(own.promoter_holding_pct / 100 if own.promoter_holding_pct is not None else None))
            oc[1].metric("Public holding", fmt_pct(own.public_holding_pct / 100 if own.public_holding_pct is not None else None))
            oc[2].metric("Promoter shares pledged",
                         fmt_pct(own.promoter_pledged_pct_of_promoter / 100) if own.promoter_pledged_pct_of_promoter is not None
                         else "n/a", help="% of the promoter's own holding that is pledged (NSE SAST disclosures).")
            _src_caption(own.source_id)
    _text_section("Overview notes", "overview_notes", "Your summary of the company.")

# ------------------------------------------------------------------ 2. business model
with tabs[1]:
    p = report.profile
    if p and p.description:
        with st.expander("Company description from data provider (unverified provider text)", expanded=True):
            st.write(p.description)
            _src_caption(p.source_id)
    else:
        st.info("No company description available from providers.", icon=":material/info:")
    _text_section("Business model", "business_model",
                  "How the company makes money: segments, customers, pricing, cost structure.")
    _text_section("Competitive advantages", "competitive_advantages",
                  "Moats: brand, scale, switching costs, network effects, licences, cost advantage. "
                  "Cite evidence and add sources in the Sources tab.")

# ------------------------------------------------------------------ 3. financials
with tabs[2]:
    fin = facts_to_frame(report.financials)
    if fin.empty:
        st.info("No financial statement data is available for this company from the configured providers. "
                "Nothing is estimated in its place.", icon=":material/cloud_off:")
    else:
        years = [f"FY{d.year}" if d.month <= 3 else f"FY{d.year + 1}" for d in fin.index]
        if len(set(years)) != len(years):
            years = [d.strftime("%b %Y") for d in fin.index]
        rows = []
        for metric, label in METRIC_LABELS.items():
            if metric not in fin.columns:
                continue
            vals = fin[metric]
            if metric in ("diluted_eps", "diluted_eps_reported"):
                fmt = lambda v: fmt_num(None if pd.isna(v) else v)
            elif metric in RATIO_METRICS:
                fmt = lambda v: fmt_pct(None if pd.isna(v) else v, 2)
            elif metric == "shares_outstanding":
                fmt = lambda v: "—" if pd.isna(v) else f"{v / 1e7:,.2f} Cr"
            else:
                fmt = lambda v: "—" if pd.isna(v) else f"{v / scale:,.0f}"
            rows.append({"Line item": label + (f" ({fin_ccy}/share)" if metric.startswith("diluted_eps") else
                                               " (%)" if metric in RATIO_METRICS else ""),
                         **{y: fmt(v) for y, v in zip(years, vals)}})
        st.markdown(f"**Annual statements** ({unit_label} unless stated)")
        if fin_ccy != "INR":
            st.warning(f"These statements are reported in **{fin_ccy}**, not INR.", icon=":material/currency_exchange:")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        counts = pd.Series([DATA_STATUS_LABELS[f.status] for f in report.financials]).value_counts()
        st.caption(" · ".join(f"{label}: {n}" for label, n in counts.items())
                   + (f" · Basis: **{report.statement_basis}** (not mixed across years)" if report.statement_basis else ""))
        with st.expander("Source filing for each year"):
            per_year = {}
            for f in report.financials:
                s = report.source_by_id(f.source_id)
                if s:
                    per_year.setdefault(f.period_end, s)
            st.dataframe(pd.DataFrame([{"Fiscal year end": d.isoformat(), "Source": s.title, "Kind": s.kind.value,
                                        "URL": s.url, "Notes": s.notes} for d, s in sorted(per_year.items(), reverse=True)]),
                         hide_index=True, width="stretch", column_config={"URL": st.column_config.LinkColumn("URL")})
            st.caption("Derived = combined from several filed lines (e.g. EBIT = profit before exceptional items "
                       "and tax + finance costs), or restated for splits/bonuses.")

        ratios = compute_ratios(fin)
        c1, c2 = st.columns(2)
        with c1:
            fig = go.Figure()
            for metric, name in (("revenue", "Revenue"), ("net_income", "Net profit")):
                if metric in fin:
                    fig.add_bar(x=years, y=fin[metric] / scale, name=name)
            fig.update_layout(height=320, title=f"Revenue and net profit ({unit_label})", barmode="group",
                              margin=dict(l=0, r=0, t=40, b=0), legend=dict(orientation="h", y=-0.15))
            st.plotly_chart(fig, width="stretch")
        with c2:
            fig = go.Figure()
            for metric in ("ebitda_margin", "ebit_margin", "net_margin", "roe", "roce"):
                if ratios[metric].notna().any():
                    fig.add_scatter(x=years, y=ratios[metric], name=RATIO_LABELS[metric], mode="lines+markers")
            fig.update_layout(height=320, title="Margins and returns", yaxis_tickformat=".0%",
                              margin=dict(l=0, r=0, t=40, b=0), legend=dict(orientation="h", y=-0.2))
            st.plotly_chart(fig, width="stretch")

        st.markdown("**Ratios** — *Derived (calculated here)* from the statements above")
        pct = {"revenue_growth", "net_income_growth", "ebitda_margin", "ebit_margin", "net_margin", "roe", "roce",
               "fcf_margin", "accruals_ratio", "effective_tax_rate"}
        rrows = []
        for metric, label in RATIO_LABELS.items():
            vals = ratios[metric]
            if metric == "fcf":
                f = lambda v: "—" if pd.isna(v) else f"{v / scale:,.0f}"
                label += f" ({unit_label})"
            elif metric in pct:
                f = lambda v: fmt_pct(None if pd.isna(v) else v)
            else:
                f = lambda v: fmt_num(None if pd.isna(v) else v)
            rrows.append({"Ratio": label, **{y: f(v) for y, v in zip(years, vals)}})
        st.dataframe(pd.DataFrame(rrows), hide_index=True, width="stretch")
        st.caption("— means the inputs were missing or the ratio is not meaningful (e.g. negative equity). "
                   "Definitions: Settings → Methodology.")

# ------------------------------------------------------------------ 4. factor analysis
with tabs[3]:
    sc = report.scorecard
    disclaimer()
    if sc is None:
        st.info("Scorecard not computed.")
    else:
        c = st.columns(3)
        gate = bool(report.readiness and report.readiness.can_conclude)
        if gate:
            c[0].metric("Framework score", f"{sc.overall_score:.0f} / 100" if sc.overall_score is not None else "—")
            c[1].metric("Interpretation", score_band(sc.overall_score))
        else:
            c[0].metric("Framework score", "Withheld", help="Shown only when the data sufficiency gate passes.")
            c[1].metric("Interpretation", "No conclusion")
        c[2].metric("Data coverage", fmt_pct(sc.coverage, 0))
        labels = [f.label for f in sc.factors]
        scores = [f.score if f.score is not None else 0 for f in sc.factors]
        fig = go.Figure(go.Bar(
            x=scores, y=labels, orientation="h",
            text=[f"{f.score:.0f}  ({f.coverage:.0%} data)" if f.score is not None else "no data"
                  for f in sc.factors], textposition="outside",
            marker_color=["#2E6FBA" if f.score is not None else "#CCCCCC" for f in sc.factors]))
        fig.update_layout(height=340, xaxis=dict(range=[0, 115], title="Factor score (0–100)"),
                          yaxis=dict(autorange="reversed"), margin=dict(l=0, r=0, t=10, b=0),
                          title=None if gate else dict(text="Provisional factor scores (gate not passed)", font=dict(size=13)))
        st.plotly_chart(fig, width="stretch")
        st.caption("Grey bars have no data. Missing metrics are excluded, never scored as zero. "
                   "Weights are set in Settings.")
        for f in sc.factors:
            with st.expander(f"{f.label} — weight {f.weight:g}, "
                             f"score {f.score:.0f}" if f.score is not None else f"{f.label} — weight {f.weight:g}, no data"):
                rules = {m.metric: m for fc in scorecard_config().factors for m in fc.metrics}
                st.dataframe(pd.DataFrame([{
                    "Metric": ms.label,
                    "Value": fmt_by_unit(ms.value, rules[ms.metric].unit) if ms.metric in rules else fmt_num(ms.value),
                    "Score": "—" if ms.score is None else f"{ms.score:.0f}",
                    "Rule": ms.rule,
                    "Note": ms.excluded_reason or km.notes.get(ms.metric, "")
                            or ("Missing" if ms.value is None else "")} for ms in f.metrics]),
                    hide_index=True, width="stretch")

# ------------------------------------------------------------------ 5. governance & risks
with tabs[4]:
    st.subheader("From filings")
    own, aud = report.ownership, report.audit
    if own or aud:
        rows_g = []
        if own:
            rows_g += [("Promoter holding", fmt_pct(own.promoter_holding_pct / 100) if own.promoter_holding_pct is not None else "—",
                        f"Shareholding pattern, {own.as_of:%d %b %Y}"),
                       ("Promoter shares pledged (% of promoter holding)",
                        fmt_pct(own.promoter_pledged_pct_of_promoter / 100) if own.promoter_pledged_pct_of_promoter is not None else "n/a",
                        "NSE pledge disclosures"),
                       ("Pledged promoter shares (% of all shares)",
                        fmt_pct(own.promoter_pledged_pct_of_total / 100) if own.promoter_pledged_pct_of_total is not None else "n/a",
                        "NSE pledge disclosures")]
            if len(own.promoter_history) > 1:
                first, last = own.promoter_history[-1], own.promoter_history[0]
                rows_g.append(("Promoter holding change", f"{last[1] - first[1]:+.2f} pp",
                               f"{first[0]:%b %Y} → {last[0]:%b %Y}"))
        if aud:
            rows_g += [("Statutory auditor(s)", ", ".join(aud.auditors) or "—", f"FY{aud.period_end.year} results filing"),
                       ("Audit opinion", aud.opinion or "—", f"FY{aud.period_end.year} results filing")]
        st.dataframe(pd.DataFrame(rows_g, columns=["Item", "Value", "Basis"]), hide_index=True, width="stretch")
        st.caption("Label: **Reported** (company filings via NSE). These take precedence over analyst entries below.")
    else:
        st.info("No governance data from filings is available.", icon=":material/info:")
    st.subheader("Analyst assessment")
    st.markdown("Governance inputs are **analyst-entered** (from shareholding patterns, annual reports, "
                "exchange disclosures). Leave blank if not assessed — blanks are treated as missing, not as zero.")
    g = report.governance
    with st.form(f"gov_{rid}"):
        c = st.columns(3)
        promoter = c[0].number_input("Promoter holding %", 0.0, 100.0, value=g.promoter_holding_pct, step=0.1)
        pledge = c[1].number_input("Promoter shares pledged %", 0.0, 100.0, value=g.promoter_pledge_pct, step=0.1,
                                   help="Used only if no filed pledge data is available.")
        auditor = c[2].text_input("Statutory auditor", value=g.auditor)
        c = st.columns(2)
        opts = [None, 1, 2, 3, 4, 5]
        gov_rating = c[0].selectbox("Governance rating (1 = poor, 5 = excellent)", opts,
                                    index=opts.index(g.governance_rating),
                                    format_func=lambda x: "Not assessed" if x is None else str(x))
        macro = c[1].selectbox("Macroeconomic resilience (1 = highly cyclical/exposed, 5 = resilient)", opts,
                               index=opts.index(g.macro_resilience_rating),
                               format_func=lambda x: "Not assessed" if x is None else str(x))
        notes = st.text_area("Governance notes (related-party transactions, auditor remarks, board, capital allocation)",
                             value=g.notes)
        if st.form_submit_button("Apply governance inputs", type="primary"):
            update_report(governance=GovernanceInputs(
                promoter_holding_pct=promoter, promoter_pledge_pct=pledge, auditor=auditor,
                governance_rating=gov_rating, macro_resilience_rating=macro, notes=notes))
            st.rerun()

    st.subheader("Key risks")
    risk_df = pd.DataFrame([r.model_dump(include={"category", "description", "severity", "mitigation"})
                            for r in report.risks] or [], columns=["category", "description", "severity", "mitigation"])
    with st.form(f"risks_{rid}"):
        edited = st.data_editor(
            risk_df, num_rows="dynamic", width="stretch", key=f"risk_editor_{rid}",
            column_config={
                "category": st.column_config.SelectboxColumn(
                    "Category", options=["Business", "Competitive", "Regulatory", "Governance", "Financial",
                                         "Macroeconomic", "Commodity / input cost", "Currency", "Technology",
                                         "ESG", "Valuation", "Other"], required=True),
                "description": st.column_config.TextColumn("Description", required=True, width="large"),
                "severity": st.column_config.SelectboxColumn("Severity", options=["low", "medium", "high"],
                                                             default="medium", required=True),
                "mitigation": st.column_config.TextColumn("Mitigation / monitoring", width="medium")})
        if st.form_submit_button("Apply risks", type="primary"):
            risks, skipped = [], 0
            for row in edited.to_dict("records"):
                if not row.get("category") or not str(row.get("description") or "").strip():
                    skipped += 1
                    continue
                risks.append(Risk(category=row["category"], description=str(row["description"]).strip(),
                                  severity=row.get("severity") or "medium", mitigation=str(row.get("mitigation") or "")))
            update_report(risks=risks)
            if skipped:
                st.session_state["save_msg"] = ("warning", f"{skipped} incomplete risk row(s) ignored.")
            st.rerun()

# ------------------------------------------------------------------ 6. valuation
with tabs[5]:
    c = st.columns(5)
    c[0].metric("P/E", fmt_x(kv.get("pe")))
    c[1].metric("P/B", fmt_x(kv.get("pb")))
    c[2].metric("EV/EBITDA", fmt_x(kv.get("ev_ebitda")))
    c[3].metric("FCF yield", fmt_pct(kv.get("fcf_yield")))
    c[4].metric("Dividend yield", fmt_pct(kv.get("dividend_yield")))
    st.caption("Multiples are *Derived* from the latest reported annual figures and current price.")

    price, eps = (report.market.price if report.market else None), kv.get("latest_eps")
    st.subheader("Bear / base / bull scenarios")
    st.caption("Scenario arithmetic on **your assumptions** — EPS grown at an assumed rate, valued at an assumed "
               "exit P/E. These are not forecasts.")
    if km.currency_mismatch:
        st.error(km.currency_mismatch, icon=":material/currency_exchange:")
    elif not price or not eps or eps <= 0:
        st.info("Scenarios need a current price and positive latest EPS"
                + ("" if price else " (no price available)")
                + ("" if eps is None or eps > 0 else " (latest EPS is negative)") + ".", icon=":material/info:")
    else:
        current_h = report.scenarios[0].horizon_years if report.scenarios else 5
        horizon = st.segmented_control("Investment horizon (years)", HORIZONS, default=current_h,
                                       key=f"h_{rid}") or current_h
        if not report.scenarios:
            suggested = suggest_scenarios(kv.get("pe"), kv.get("eps_cagr"), kv.get("revenue_cagr"), horizon,
                                          kv.get("dividend_yield"))
            if suggested:
                st.markdown("No assumptions yet. You can start from values derived from this company's own "
                            "history, then edit them.")
                if st.button("Use history-based starting assumptions", icon=":material/auto_fix_high:"):
                    update_report(scenarios=suggested)
                    st.rerun()
            else:
                st.markdown("Not enough history to suggest assumptions — enter your own below.")
        base = {a.scenario: a for a in report.scenarios}
        editor_df = pd.DataFrame([{
            "scenario": s,
            "eps_growth_pct": round(base[s].eps_growth * 100, 2) if s in base else None,
            "exit_pe": base[s].exit_pe if s in base else None,
            "dividend_yield_pct": round(base[s].dividend_yield * 100, 2) if s in base else 0.0,
            "rationale": base[s].rationale if s in base else ""} for s in ("bear", "base", "bull")])
        with st.form(f"scen_{rid}_{horizon}"):
            edited = st.data_editor(editor_df, hide_index=True, width="stretch", column_config={
                "scenario": st.column_config.TextColumn("Scenario", disabled=True),
                "eps_growth_pct": st.column_config.NumberColumn("EPS growth %/yr", min_value=-90.0, max_value=200.0, step=0.5),
                "exit_pe": st.column_config.NumberColumn("Exit P/E", min_value=0.1, max_value=500.0, step=0.5),
                "dividend_yield_pct": st.column_config.NumberColumn("Dividend yield %", min_value=0.0, max_value=50.0, step=0.1),
                "rationale": st.column_config.TextColumn("Rationale", width="large")})
            if st.form_submit_button(f"Apply assumptions ({horizon}-year horizon)", type="primary"):
                scenarios, errs = [], []
                for row in edited.to_dict("records"):
                    if pd.isna(row["eps_growth_pct"]) or pd.isna(row["exit_pe"]):
                        continue
                    try:
                        scenarios.append(ScenarioAssumption(
                            scenario=row["scenario"], horizon_years=horizon, eps_growth=row["eps_growth_pct"] / 100,
                            exit_pe=row["exit_pe"], dividend_yield=(row["dividend_yield_pct"] or 0) / 100,
                            rationale=row["rationale"] or ""))
                    except ValidationError as exc:
                        errs.append(f"{row['scenario']}: {exc.errors()[0]['msg']}")
                if errs:
                    st.error("; ".join(errs))
                else:
                    update_report(scenarios=scenarios)
                    st.rerun()
        if report.scenario_results:
            res = report.scenario_results
            st.dataframe(pd.DataFrame([{
                "Scenario": r.scenario, "Horizon": f"{r.horizon_years}y", "Start EPS": fmt_inr(r.start_eps),
                "End EPS": fmt_inr(r.end_eps), "Target price": fmt_inr(r.target_price),
                "Total return": fmt_pct(r.total_return), "Expected CAGR": fmt_pct(r.cagr)} for r in res]),
                hide_index=True, width="stretch")
            colors = {"bear": "#C0392B", "base": "#2E6FBA", "bull": "#1E8449"}
            fig = go.Figure(go.Bar(x=[r.scenario for r in res], y=[r.cagr for r in res],
                                   marker_color=[colors[r.scenario] for r in res],
                                   text=[fmt_pct(r.cagr) for r in res], textposition="outside"))
            fig.update_layout(height=280, yaxis_tickformat=".0%", title="Expected CAGR by scenario (assumption-driven)",
                              margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, width="stretch")

    with st.expander("DCF and reverse DCF calculator (assumptions are not saved in Phase 1)"):
        fcf0, mkt = kv.get("latest_fcf"), report.market
        fin = facts_to_frame(report.financials)
        debt = fin["total_debt"].dropna().iloc[-1] if "total_debt" in fin and fin["total_debt"].notna().any() else None
        cash = (fin["cash_and_st_investments"].dropna().iloc[-1]
                if "cash_and_st_investments" in fin and fin["cash_and_st_investments"].notna().any() else None)
        shares = mkt.shares_outstanding if mkt else None
        if report.company_type in ("bank", "nbfc", "insurer"):
            st.info("A free-cash-flow DCF is not meaningful for lenders and insurers (borrowing and lending "
                    "are their operations). Use P/B against sustainable ROE instead; a residual-income model "
                    "is a planned extension.")
        elif km.currency_mismatch:
            st.info("DCF disabled: " + km.currency_mismatch)
        elif fcf0 is None or shares is None or debt is None or cash is None:
            st.info("DCF needs latest free cash flow, debt, cash and shares outstanding; some are missing.")
        else:
            st.caption(f"Starting FCF {fmt_cr(fcf0)} · net debt {fmt_cr(debt - cash)} · shares "
                       f"{shares / 1e7:,.2f} Cr — *Derived* from reported data. Rates below are **your assumptions**.")
            c = st.columns(4)
            g1 = c[0].number_input("FCF growth %/yr (stage 1)", -50.0, 100.0, 10.0, 0.5, key=f"dcf_g_{rid}") / 100
            yrs = c[1].number_input("Stage-1 years", 1, 20, 10, key=f"dcf_n_{rid}")
            tg = c[2].number_input("Terminal growth %", -5.0, 10.0, 5.0, 0.25, key=f"dcf_tg_{rid}") / 100
            dr = c[3].number_input("Discount rate %", 1.0, 30.0, 12.0, 0.25, key=f"dcf_dr_{rid}") / 100
            try:
                res = dcf_value(fcf0, g1, int(yrs), tg, dr, debt - cash, shares)
                c = st.columns(3)
                c[0].metric("Value per share (assumption-driven)", fmt_inr(res.per_share))
                c[1].metric("Terminal value share of EV", fmt_pct(res.terminal_share, 0))
                if price:
                    implied = reverse_dcf_implied_growth(price, fcf0, int(yrs), tg, dr, debt - cash, shares)
                    c[2].metric("Reverse DCF: growth implied by price",
                                fmt_pct(implied) if implied is not None else "No solution in −50%…100%")
                if fcf0 <= 0:
                    st.warning("Latest FCF is negative; DCF output is not meaningful.")
            except ValueError as exc:
                st.error(str(exc))

# ------------------------------------------------------------------ 7. sources & data quality
with tabs[6]:
    v = report.verification
    st.subheader("Identity verification")
    if v:
        st.write(v.summary)
        st.dataframe(pd.DataFrame([{"Check": c.name, "Result": {True: "✅ Pass", False: "❌ Fail", None: "➖ Not checked"}[c.passed],
                                    "Detail": c.detail} for c in v.checks]), hide_index=True, width="stretch")
    st.subheader("Data-quality warnings")
    if report.warnings:
        icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
        order = {"critical": 0, "warning": 1, "info": 2}
        for w in sorted(report.warnings, key=lambda w: order[w.severity]):
            st.markdown(f"{icon[w.severity]} **{w.section}** — {w.message}")
    else:
        st.write("No warnings.")
    st.subheader("Sources")
    st.dataframe(pd.DataFrame([{"Title": s.title, "Provider": s.provider, "Kind": s.kind.value.replace("_", " "),
                                "URL": s.url, "Retrieved (UTC)": s.retrieved_at.strftime("%Y-%m-%d %H:%M"),
                                "Notes": s.notes} for s in report.sources]),
                 hide_index=True, width="stretch",
                 column_config={"URL": st.column_config.LinkColumn("URL")})
    with st.form(f"add_src_{rid}", clear_on_submit=True):
        st.markdown("**Add a source you used** (annual report, exchange filing, concall transcript…)")
        c = st.columns([2, 2, 2])
        title = c[0].text_input("Title")
        url = c[1].text_input("URL (http/https)")
        note = c[2].text_input("Notes")
        if st.form_submit_button("Add source"):
            try:
                if not title.strip():
                    raise ValueError("Title is required")
                src = Source(title=title.strip(), provider="Analyst", kind=SourceKind.USER_SUPPLIED,
                             url=url or None, notes=note)
                update_report(sources=report.sources + [src])
                st.rerun()
            except (ValidationError, ValueError) as exc:
                st.error(f"Source not added: {exc}")
    status_legend()

# ------------------------------------------------------------------ save
st.divider()


CONFIRM_KEY = "confirm_save_open"


def _dismissed() -> None:
    st.session_state[CONFIRM_KEY] = False
    st.session_state["save_msg"] = ("info", "Not saved. The research database and workbook were not modified.")


@st.dialog("Save completed research report?", on_dismiss=_dismissed)
def confirm_save() -> None:
    r = current_report()
    gate = r.readiness.can_conclude if r.readiness else False
    st.markdown(f"**{r.identity.company_name}** ({r.identity.key})")
    st.markdown(f"- Verification: **{r.verification.status.value if r.verification else 'unverified'}**\n"
                f"- Data coverage: **{fmt_pct(r.scorecard.coverage if r.scorecard else 0, 0)}**\n"
                f"- Conclusion gate: **{'passed' if gate else 'not passed — saved as incomplete research'}**\n"
                f"- {len(r.financials)} financial facts, {len(r.risks)} risks, {len(r.scenarios)} scenarios, "
                f"{len(r.sources)} sources")
    st.markdown("Do you want to save this completed report to the research database? **Yes** adds a new "
                "version to the SQLite database and updates `data/Equity_Intelligence_India.xlsx`; previous "
                "companies and research history are kept. **No** leaves both unchanged.")
    c1, c2 = st.columns(2)
    if c1.button("Yes, save", type="primary", width="stretch"):
        st.session_state[CONFIRM_KEY] = False
        try:
            outcome = save_completed_report(r, get_app().repo, get_app().workbook)
        except (DatabaseError, ValueError) as exc:
            st.session_state["save_msg"] = ("error", f"Not saved: {exc}")
        else:
            if outcome.excel_ok:
                msg = ("success", f"Saved as version {outcome.saved.version} to the database and Excel workbook.")
            else:
                msg = ("warning", f"Saved to the database as version {outcome.saved.version}, but the Excel "
                                  f"update failed: {outcome.excel_error} Use **Settings → Retry Excel sync**.")
            st.session_state["save_msg"] = msg
            update_report(status="completed")
        st.rerun()
    if c2.button("No, don't save", width="stretch"):
        st.session_state[CONFIRM_KEY] = False
        st.session_state["save_msg"] = ("info", "Not saved. The research database and workbook were not modified.")
        st.rerun()


c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
analyst_key = f"analyst_{rid}"
c1.text_input("Analyst name (optional)", value=report.analyst, key=analyst_key,
              on_change=lambda: update_report(analyst=st.session_state[analyst_key]))
if c2.button("Complete & save report…", type="primary", icon=":material/save:", width="stretch"):
    st.session_state[CONFIRM_KEY] = True
if st.session_state.get(CONFIRM_KEY):
    confirm_save()
