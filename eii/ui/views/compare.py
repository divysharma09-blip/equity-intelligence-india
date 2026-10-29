import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from eii.services import compute_key_metrics
from eii.ui.components import disclaimer, fmt_cr, fmt_inr, fmt_pct, fmt_x
from eii.ui.state import current_report, get_app

st.title(":material/compare_arrows: Compare companies")
repo = get_app().repo
companies = repo.list_companies()

options: dict[str, object] = {}
for _, c in companies.iterrows():
    if c["latest_report_id"]:
        options[f"{c['company_name']} (saved v{int(c['latest_version'])})"] = c["latest_report_id"]
draft = current_report()
if draft is not None:
    options[f"{draft.identity.company_name} (current workspace)"] = "__draft__"

if len(options) < 2:
    st.info("Comparison needs at least two companies: save research for two companies, or save one and "
            "keep another open in the workspace.", icon=":material/info:")
    st.stop()

chosen = st.multiselect("Companies (latest saved version of each)", list(options), default=list(options)[:3],
                        max_selections=6)
if len(chosen) < 2:
    st.caption("Select at least two.")
    st.stop()

pairs = [(k, draft if options[k] == "__draft__" else repo.get_report(options[k])) for k in chosen]
pairs = [(k, r) for k, r in pairs if r is not None]
reports = [r for _, r in pairs]

rows = []
for label, r in pairs:
    v = compute_key_metrics(r).values
    sc = r.scorecard
    gate = bool(r.readiness and r.readiness.can_conclude)
    rows.append({
        "Company": label, "Sector": r.profile.sector if r.profile else "—",
        "Verification": r.verification.status.value if r.verification else "unverified",
        "Price": fmt_inr(r.market.price if r.market else None), "Market cap": fmt_cr(v.get("market_cap")),
        "P/E": fmt_x(v.get("pe")), "P/B": fmt_x(v.get("pb")), "EV/EBITDA": fmt_x(v.get("ev_ebitda")),
        "FCF yield": fmt_pct(v.get("fcf_yield")), "ROE 3y": fmt_pct(v.get("roe_avg_3y")),
        "ROCE 3y": fmt_pct(v.get("roce_avg_3y")), "Revenue CAGR": fmt_pct(v.get("revenue_cagr")),
        "EPS CAGR": fmt_pct(v.get("eps_cagr")), "Debt/Equity": fmt_x(v.get("debt_to_equity"), 2),
        "Framework score": (f"{sc.overall_score:.0f}" if gate and sc and sc.overall_score is not None
                            else "Withheld"),
        "Coverage": fmt_pct(sc.coverage if sc else None, 0), "Gate": "Passed" if gate else "Not passed",
        "Data as of": r.updated_at.strftime("%Y-%m-%d")})
table = pd.DataFrame(rows).set_index("Company").T
st.dataframe(table, width="stretch")
st.caption("All multiples and ratios are *Derived* from each report's stored data at the date shown. "
           "Reports saved on different dates are not like-for-like. Industrial ratios are not comparable "
           "with banks/NBFCs.")

sectors = {r.profile.sector for r in reports if r.profile and r.profile.sector}
if len(sectors) > 1:
    st.warning(f"Companies span {len(sectors)} sectors ({', '.join(sorted(sectors))}). Cross-sector comparison "
               "of valuation and margin metrics can mislead.", icon=":material/warning:")

st.subheader("Factor profile")
disclaimer()
fig = go.Figure()
for label, r in pairs:
    if not r.scorecard:
        continue
    labels = [f.label for f in r.scorecard.factors]
    vals = [f.score for f in r.scorecard.factors]
    fig.add_scatterpolar(r=vals + vals[:1], theta=labels + labels[:1], name=label,
                         connectgaps=False, mode="lines+markers")
fig.update_layout(height=480, polar=dict(radialaxis=dict(range=[0, 100])), legend=dict(orientation="h", y=-0.1),
                  margin=dict(l=40, r=40, t=20, b=40))
st.plotly_chart(fig, width="stretch")
st.caption("Gaps in a line mean that factor had no data for that company — it is not plotted as zero. "
           "Factor scores are shown even where the conclusion gate has not passed; treat those as provisional.")
