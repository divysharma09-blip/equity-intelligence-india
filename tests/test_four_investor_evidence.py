import io
import zipfile
from datetime import date

import pandas as pd
import pytest

from eii.calculations.financials import KeyMetrics
from eii.four_investor.evidence import Evidence, parse_brsr, summarise_announcements
from eii.four_investor.framework import (PARAMS, Rating, app_ratings, dcf_estimates, derived_metrics, merge,
                                         score_investor, sector_ratings)
from eii.four_investor.sector_profiles import PROFILE_PARAMS, PROFILES, resolve_sector
from eii.providers.nse_bhavcopy import parse_bhavcopy, url_for

BRSR = b"""<?xml version="1.0"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
            xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/in-capmkt">
  <xbrli:context id="D2026"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="D2025"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="Loc"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:LocationAxis">in-capmkt:LocationMember</xbrldi:explicitMember>
    <xbrldi:explicitMember dimension="in-capmkt:GeographicalAxis">in-capmkt:NationalMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="Att"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:GenderAxis">in-capmkt:GenderMember</xbrldi:explicitMember>
    <xbrldi:explicitMember dimension="in-capmkt:PermanentEmployeesAndWorkersAxis">in-capmkt:PermanentEmployeesMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
  <in-capmkt:PercentageOfContributionOfExportsInTheTotalTurnoverOfTheEntity contextRef="D2026">0.12</in-capmkt:PercentageOfContributionOfExportsInTheTotalTurnoverOfTheEntity>
  <in-capmkt:PercentageOfContributionOfExportsInTheTotalTurnoverOfTheEntity contextRef="D2025">0.40</in-capmkt:PercentageOfContributionOfExportsInTheTotalTurnoverOfTheEntity>
  <in-capmkt:PercentageOfRAndD contextRef="D2026">0.02</in-capmkt:PercentageOfRAndD>
  <in-capmkt:PercentageOfPurchasesFromRelatedPartiesInTotalPurchasesForShareOfRelatedPartyTransactions contextRef="D2026">0.3</in-capmkt:PercentageOfPurchasesFromRelatedPartiesInTotalPurchasesForShareOfRelatedPartyTransactions>
  <in-capmkt:NumberOfLocations contextRef="Loc">42</in-capmkt:NumberOfLocations>
  <in-capmkt:TurnoverRate contextRef="Att">0.15</in-capmkt:TurnoverRate>
</xbrli:xbrl>"""


def test_parse_brsr_takes_latest_year_and_dimensioned_totals():
    v = parse_brsr(BRSR)
    assert v["exports_pct"] == 0.12 and v["rnd_pct"] == 0.02 and v["rpt_purchases_pct"] == 0.3
    assert v["locations_national"] == 42 and v["attrition"] == 0.15 and v["brsr_year_end"] == 2026


def test_announcement_summary_classifies_red_flags_and_communication():
    rows = [
        {"an_dt": "10-Oct-2026 10:00:00", "desc": "Fraud/Default/Arrest", "attchmntText": "default by subsidiary"},
        {"an_dt": "01-Sep-2026 10:00:00", "desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
         "attchmntText": "Earnings call transcript"},
        {"an_dt": "01-Aug-2026 10:00:00", "desc": "Updates", "attchmntText": "Investor Presentation for Q1"},
        {"an_dt": "01-Jan-2024 10:00:00", "desc": "Updates", "attchmntText": "Investor Presentation (old)"},
        {"an_dt": "05-May-2025 10:00:00", "desc": "Resignation", "attchmntText": "Resignation of Statutory Auditor"},
        {"an_dt": "05-May-2025 10:00:00", "desc": "Resignation of Director/KMP/SMP", "attchmntText": "CFO resigned"},
        {"an_dt": "05-May-2025 10:00:00", "desc": "Acquisition", "attchmntText": "acquired X"},
        {"an_dt": "05-May-2025 10:00:00", "desc": "Credit Rating- Revision", "attchmntText": "ICRA downgraded"},
    ]
    s = summarise_announcements(rows, date(2026, 10, 31))
    assert s["red_flag_events"] == 1 and s["auditor_resignations"] == 1 and s["kmp_resignations"] == 1
    assert s["investor_comms_12m"] == 2 and s["acquisitions_3y"] == 1 and s["rating_downgrades"] == 1


def test_legacy_bhavcopy_is_read_like_udiff():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cm31MAR2023bhav.csv", "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,"
                   "TIMESTAMP,TOTALTRADES,ISIN,\nJPPOWER,EQ,6,6,6,6.05,6,6,1,1,31-MAR-2023,1,INE351F01018,\n")
    df = parse_bhavcopy(buf.getvalue())
    assert df.iloc[0]["TckrSymb"] == "JPPOWER" and df.iloc[0]["ClsPric"] == pytest.approx(6.05)
    assert "historical/EQUITIES/2023/MAR/cm31MAR2023bhav" in url_for(date(2023, 3, 31))
    assert "BhavCopy_NSE_CM_0_0_0_20250328" in url_for(date(2025, 3, 28))


def test_sector_profiles_are_complete_and_resolve_nse_tags():
    assert all(len(p.ratings) == len(PROFILE_PARAMS) and all(0 <= r <= 4 for r in p.ratings) for p in PROFILES.values())
    assert resolve_sector("Computers - Software") == "Information Technology"
    assert resolve_sector("Power") == "Power" and resolve_sector("Banks") == "Financial Services"
    assert resolve_sector("Something else") is None


def test_sector_ratings_mark_inapplicable_parameters_and_patents():
    name, power = sector_ratings("Power", "industrial")
    assert name == "Power" and not power["fisher.innovation.patents"].applicable
    assert not power["buffett.moat.brand"].applicable and power["dalio.macro.government"].rating == 1
    _, it = sector_ratings("Information Technology", "industrial", Evidence(values={"rnd_pct": 0.02}))
    assert not it["fisher.innovation.patents"].applicable and it["fisher.innovation.rnd"].rating == 3
    _, pharma = sector_ratings("Healthcare", "industrial")
    assert "fisher.innovation.patents" not in pharma  # applicable, left for research


def test_not_applicable_is_excluded_from_coverage_and_model_yields_to_claude():
    na = {p.id: Rating(param_id=p.id, applicable=False, source="sector") for p in PARAMS
          if p.investor == "fisher" and p.category == "Innovation and R&D"}
    rated = {p.id: Rating(param_id=p.id, rating=2) for p in PARAMS
             if p.investor == "fisher" and p.category != "Innovation and R&D"}
    s = score_investor("fisher", {**na, **rated})
    assert s.applicable_weight == 75 and s.coverage == 1 and s.score == 50
    app = {"buffett.value.dcf": Rating(param_id="buffett.value.dcf", rating=4, source="model"),
           "buffett.economics.roe": Rating(param_id="buffett.economics.roe", rating=4)}
    claude = {k: Rating(param_id=k, rating=1, source="claude") for k in app}
    m = merge(app, claude, {"buffett.value.dcf": Rating(param_id="buffett.value.dcf", rating=0, source="sector")})
    assert m["buffett.value.dcf"].rating == 1 and m["buffett.economics.roe"].rating == 4


def _fin(**cols):
    idx = pd.to_datetime([f"{y}-03-31" for y in range(2020, 2020 + len(next(iter(cols.values()))))])
    return pd.DataFrame(cols, index=idx)


def test_derived_metrics_for_inflation_recession_services_and_refinancing():
    fin = _fin(revenue=[100, 80, 120, 130, 150], ebit=[20, 12, 24, 22, 30], employee_cost=[50, 45, 60, 62, 70],
               total_debt=[100, 100, 100, 100, 80], short_term_debt=[10, 10, 10, 10, 40],
               total_equity=[100, 100, 100, 100, 100], net_income=[10, 5, 12, 11, 15])
    d = derived_metrics(fin)
    assert d["covid_revenue_change"] == pytest.approx(-0.2)
    assert d["inflation_margin_change"] == pytest.approx(22 / 130 - 24 / 120)
    assert d["services_ebit_margin"] == pytest.approx((24 / 120 + 22 / 130 + 30 / 150) / 3)
    assert d["employee_cost_share"] == pytest.approx((60 / 120 + 62 / 130 + 70 / 150) / 3)
    assert d["short_term_debt_share"] == pytest.approx(0.5) and "gross_margin" not in d
    shrink = derived_metrics(_fin(ebit=[10, 12, 14, 18], total_equity=[100, 90, 80, 70], total_debt=[0, 0, 0, 0]))
    assert shrink["incremental_roce"] == 1.0  # profit up while capital employed fell


def test_dcf_estimates_and_negative_fcf():
    fin = _fin(operating_cash_flow=[100e7, 110e7, 120e7], capex=[-20e7, -20e7, -20e7], shares_outstanding=[10e7] * 3,
               total_debt=[0.0] * 3, cash_and_st_investments=[0.0] * 3)
    km = KeyMetrics(values={"revenue_cagr": 0.08})
    vals, why = dcf_estimates(fin, km, price=100.0)
    assert vals["dcf_base_value"] > vals["dcf_bear_value"] > 0 and "Base: 8.0% growth" in why
    neg = fin.assign(capex=[-200e7] * 3)
    assert dcf_estimates(neg, km, 100.0)[0]["dcf_upside"] == -1.0


def test_app_ratings_use_evidence_and_lender_substitutes():
    ev = Evidence(values={"exports_pct": 0.6, "attrition": 0.05, "rpt_purchases_pct": 0.3, "rpt_sales_pct": 0.01,
                          "red_flag_events": 0, "regulatory_actions": 0, "promoter_holding_change": -12.0,
                          "auditor_resignations": 0, "acquisitions_3y": 0, "investor_comms_12m": 5, "index_tier": 4})
    r = app_ratings(KeyMetrics(values={"audit_opinion_clean": 1.0}), "industrial", None, None, ev)
    assert r["dalio.macro.international"].rating == 0 and r["fisher.execution.talent"].rating == 4
    assert r["munger.integrity.related_party"].rating == 1 and r["munger.integrity.track_record"].rating == 2
    assert r["munger.integrity.auditor"].rating == 4 and r["munger.capital.acquisitions"].rating == 3
    assert r["munger.integrity.transparency"].rating == 3 and r["fisher.competition.positioning"].rating == 4
    bank = app_ratings(KeyMetrics(values={"cet1_ratio": 0.14, "nii_to_assets": 0.035}), "bank")
    assert bank["munger.protection.survival"].rating == 3 and bank["dalio.rates.rate_sensitivity"].rating == 3
    assert not bank["buffett.strength.fcf"].applicable
    no_promoter = app_ratings(KeyMetrics(), "bank", None, None, Evidence(values={"promoter_holding": 0.0}))
    assert not no_promoter["munger.integrity.pledging"].applicable
