"""NSE filings provider: XBRL parsing, mapping, corporate actions, ownership, merge.

XBRL documents here are SYNTHETIC, shaped like the three real variants observed on
NSE (integrated 2025+, legacy with mis-dated contexts, legacy with undefined contexts).
All figures are fictional.
"""

from datetime import date

import pytest

from eii.models import DataStatus, FinancialFact, SecurityIdentity
from eii.providers.base import FundamentalsBundle, ProviderStatus, ProviderUnavailable
from eii.providers.nse_filings import (FilingRef, NSEClient, NSEFilingsProvider, adjust_per_share,
                                       annual_filing_refs, choose_basis, map_bank, map_industrial,
                                       map_nbfc, parse_corporate_actions, parse_ownership,
                                       sanity_check, share_multiplier)
from eii.providers.xbrl import XbrlParseError, detect_format, parse_results_xbrl
from eii.services.research import reconcile

NS = 'xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:x="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"'


def ctx(cid, start=None, end=None, instant=None, dims=False):
    period = (f"<xbrli:instant>{instant}</xbrli:instant>" if instant else
              f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>")
    seg = "<xbrli:segment><d>x</d></xbrli:segment>" if dims else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">ABC</xbrli:identifier>'
            f"{seg}</xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>")


def doc(contexts: str, facts: dict[tuple[str, str], object], ns_hint: str = "IntegratedFinance_IndAS") -> bytes:
    body = "".join(f'<x:{n} contextRef="{c}">{v}</x:{n}>' for (n, c), v in facts.items())
    return (f'<?xml version="1.0"?><xbrli:xbrl {NS} xmlns:hint="http://x/{ns_hint}">'
            f"{contexts}{body}</xbrli:xbrl>").encode()


INDUSTRIAL_FY26 = doc(
    ctx("OneD", "2026-01-01", "2026-03-31") + ctx("FourD", "2025-04-01", "2026-03-31") +
    ctx("OneI", instant="2026-03-31") + ctx("SegD", "2025-04-01", "2026-03-31", dims=True),
    {("NatureOfReportStandaloneConsolidated", "FourD"): "Consolidated",
     ("DescriptionOfPresentationCurrency", "OneD"): "INR",
     ("DateOfStartOfReportingPeriod", "FourD"): "2025-04-01",
     ("DateOfEndOfReportingPeriod", "FourD"): "2026-03-31",
     ("RevenueFromOperations", "OneD"): 300, ("RevenueFromOperations", "FourD"): 1000,
     ("RevenueFromOperations", "SegD"): 999999,
     ("FinanceCosts", "FourD"): 20, ("DepreciationDepletionAndAmortisationExpense", "FourD"): 50,
     ("ProfitBeforeExceptionalItemsAndTax", "FourD"): 180, ("ProfitBeforeTax", "FourD"): 170,
     ("TaxExpense", "FourD"): 40, ("ProfitOrLossAttributableToOwnersOfParent", "FourD"): 130,
     ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "FourD"): "0.00",
     ("DilutedEarningsLossPerShareFromContinuingOperations", "FourD"): 13,
     ("PaidUpValueOfEquityShareCapital", "FourD"): 100, ("FaceValueOfEquityShareCapital", "FourD"): 10,
     ("CashFlowsFromUsedInOperatingActivities", "FourD"): 150,
     ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "FourD"): 60,
     ("PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", "FourD"): 5,
     ("DividendsPaidClassifiedAsFinancingActivities", "FourD"): 30,
     ("DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification", "OneD"): "Declaration of unmodified opinion",
     ("AuditorsFirmName", "AudCtx"): "Fictional &amp; Co LLP",
     ("Assets", "OneI"): 2000, ("EquityAndLiabilities", "OneI"): 2000,
     ("EquityAttributableToOwnersOfParent", "OneI"): 900, ("Equity", "OneI"): 950,
     ("BorrowingsNoncurrent", "OneI"): 200, ("BorrowingsCurrent", "OneI"): 50,
     ("CashAndCashEquivalents", "OneI"): 70, ("CurrentInvestments", "OneI"): 30,
     ("CurrentAssets", "OneI"): 800, ("CurrentLiabilities", "OneI"): 400})

# Legacy: FourD context carries quarter dates; the facts inside declare the full year.
LEGACY_MISDATED = doc(
    ctx("OneD", "2024-01-01", "2024-03-31") + ctx("FourD", "2024-01-01", "2024-03-31") +
    ctx("OneI", instant="2024-03-31"),
    {("DateOfStartOfReportingPeriod", "FourD"): "2023-04-01",
     ("DateOfEndOfReportingPeriod", "FourD"): "2024-03-31",
     ("RevenueFromOperations", "OneD"): 250, ("RevenueFromOperations", "FourD"): 900,
     ("Assets", "OneI"): 1800}, ns_hint="in-bse-fin")

# Legacy bank: referenced contexts are never defined.
LEGACY_UNDEFINED_BANK = doc(
    "",
    {("DateOfEndOfFinancialYear", "OneD"): "2023-03-31",
     ("DateOfEndOfReportingPeriod", "FourD"): "2023-03-31",
     ("InterestEarned", "FourD"): 500, ("InterestExpended", "FourD"): 300,
     ("PercentageOfGrossNpa", "FourD"): 0.02, ("ReturnOnAssets", "FourD"): 7.5},
    ns_hint="banking_entry_point")


# ------------------------------------------------------------------ XBRL parser

def test_parse_integrated_selects_annual_and_instant_ignoring_dimensions():
    f = parse_results_xbrl(INDUSTRIAL_FY26, "INTEGRATED_FILING_INDAS_1.xml")
    assert f.format == "industrial" and f.basis == "Consolidated" and f.currency == "INR"
    assert f.period_start == date(2025, 4, 1) and f.period_end == date(2026, 3, 31)
    assert f.annual["RevenueFromOperations"] == 1000          # not the quarter (300) or segment
    assert f.instant["Assets"] == 2000
    assert f.lists["AuditorsFirmName"] == ["Fictional & Co LLP"]
    assert f.rejected == []


def test_parse_legacy_misdated_uses_declared_period():
    f = parse_results_xbrl(LEGACY_MISDATED, "INDAS_1.xml")
    assert f.annual["RevenueFromOperations"] == 900
    assert f.period_start == date(2023, 4, 1)


def test_parse_legacy_undefined_contexts_uses_convention_and_rejects_bad_ratio():
    f = parse_results_xbrl(LEGACY_UNDEFINED_BANK, "BANKING_1.xml")
    assert f.format == "bank"
    assert f.annual["InterestEarned"] == 500
    assert "ReturnOnAssets" not in f.annual                     # 7.5 is not a decimal ratio
    assert any("ReturnOnAssets" in r for r in f.rejected)
    assert any("legacy context conventions" in r for r in f.rejected)


def test_parse_errors():
    with pytest.raises(XbrlParseError):
        parse_results_xbrl(b"not xml")
    with pytest.raises(XbrlParseError):
        parse_results_xbrl(doc("", {}))


@pytest.mark.parametrize("name,expected", [
    ("INTEGRATED_FILING_BANKING_1.xml", "bank"), ("INTEGRATED_FILING_NBFC_INDAS_1.xml", "nbfc"),
    ("INTEGRATED_FILING_LI_1.xml", "insurer"), ("INDAS_83500_1.xml", "industrial"), ("x.xml", "unknown")])
def test_detect_format(name, expected):
    assert detect_format(name) == expected

# ------------------------------------------------------------------ mapping


def facts_of(mapped):
    return {m: (v, st) for m, v, st, _ in mapped}


def test_map_industrial_reported_vs_derived():
    m = facts_of(map_industrial(parse_results_xbrl(INDUSTRIAL_FY26, "INDAS.xml"), "INR"))
    assert m["revenue"] == (1000, DataStatus.REPORTED)
    assert m["net_income"] == (130, DataStatus.REPORTED)
    assert m["diluted_eps"][0] == 13          # 0.00 placeholder skipped for the filled element
    assert m["ebit"] == (200, DataStatus.DERIVED)               # PBEIT 180 + finance cost 20
    assert m["ebitda"] == (250, DataStatus.DERIVED)
    assert m["total_equity"][0] == 900                          # owners of parent, not incl. NCI
    assert m["total_debt"] == (250, DataStatus.DERIVED)
    assert m["cash_and_st_investments"] == (100, DataStatus.DERIVED)
    assert m["capex"] == (-65, DataStatus.DERIVED)
    assert m["dividends_paid"][0] == -30
    assert m["shares_outstanding"] == (10, DataStatus.DERIVED)


def test_zero_eps_with_profit_is_treated_as_missing():
    f = parse_results_xbrl(doc(ctx("FourD", "2025-04-01", "2026-03-31"), {
        ("ProfitLossForPeriod", "FourD"): 100,
        ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "FourD"): 0}), "INDAS.xml")
    assert "diluted_eps" not in facts_of(map_industrial(f, "INR"))


def test_map_bank_zero_ratios_are_missing_and_nii_derived():
    f = parse_results_xbrl(doc(ctx("FourD", "2025-04-01", "2026-03-31") + ctx("OneI", instant="2026-03-31"), {
        ("InterestEarned", "FourD"): 1000, ("InterestExpended", "FourD"): 600, ("Income", "FourD"): 1200,
        ("ProfitLossForThePeriod", "FourD"): 150, ("PercentageOfGrossNpa", "FourD"): 0,
        ("CET1Ratio", "FourD"): 0, ("ReturnOnAssets", "FourD"): 0.018,
        ("Capital", "OneI"): 10, ("ReservesAndSurplus", "OneI"): 990, ("Advances", "OneI"): 7000,
        ("Deposits", "OneI"): 8000}), "BANKING.xml")
    m = facts_of(map_bank(f, "INR"))
    assert m["net_interest_income"] == (400, DataStatus.DERIVED)
    assert m["total_equity"] == (1000, DataStatus.DERIVED)
    assert "gnpa_pct" not in m and "cet1_ratio" not in m and "nnpa_pct" not in m
    assert m["roa_reported"][0] == 0.018


def test_map_nbfc():
    f = parse_results_xbrl(doc(ctx("FourD", "2025-04-01", "2026-03-31") + ctx("OneI", instant="2026-03-31"), {
        ("InterestEarned", "FourD"): 800, ("FinanceCosts", "FourD"): 350,
        ("ImpairmentOnFinancialInstruments", "FourD"): 40, ("RevenueFromOperations", "FourD"): 900,
        ("Loans", "OneI"): 5000, ("DebtSecurities", "OneI"): 2000, ("Borrowings", "OneI"): 1500}),
        "NBFC_INDAS.xml")
    m = facts_of(map_nbfc(f, "INR"))
    assert m["net_interest_income"] == (450, DataStatus.DERIVED)
    assert m["advances"][0] == 5000 and m["provisions"][0] == 40
    assert m["total_debt"] == (3500, DataStatus.DERIVED)


def test_sanity_check_drops_inconsistent_profit_and_eps():
    f = parse_results_xbrl(INDUSTRIAL_FY26, "INDAS.xml")
    facts = [("net_income", 400.0, DataStatus.REPORTED, "INR"), ("diluted_eps", 13.0, DataStatus.REPORTED, "INR/share"),
             ("shares_outstanding", 10.0, DataStatus.DERIVED, "shares"), ("revenue", 1000.0, DataStatus.REPORTED, "INR")]
    kept, notes = sanity_check(f, facts)
    assert {m for m, *_ in kept} == {"shares_outstanding", "revenue"}
    assert "internally inconsistent" in notes[0]
    ok, notes = sanity_check(f, [("net_income", 130.0, DataStatus.REPORTED, "INR"), *facts[1:]])
    assert len(ok) == 4 and notes == []

# ------------------------------------------------------------------ listings


def test_filing_refs_prefer_integrated_and_latest_revision():
    legacy = [
        {"toDate": "31-Mar-2024", "consolidated": "Consolidated", "xbrl": "https://x/old.xml",
         "broadCastDate": "12-Apr-2024 21:01:55"},
        {"toDate": "31-Mar-2024", "consolidated": "Consolidated", "xbrl": "https://x/revised.xml",
         "broadCastDate": "20-Apr-2024 10:00:00"},
        {"toDate": "31-Mar-2023", "consolidated": "Non-Consolidated", "xbrl": "https://x/sa23.xml",
         "broadCastDate": "13-Apr-2023 12:45:46"},
        {"toDate": "31-Mar-2016", "consolidated": "Consolidated", "xbrl": "https://x/-", "broadCastDate": None}]
    integrated = [
        {"qe_Date": "31-MAR-2025", "consolidated": "Consolidated", "xbrl": "https://x/int25.xml",
         "broadcast_Date": "11-Apr-2025 07:41:37"},
        {"qe_Date": "30-JUN-2025", "consolidated": "Consolidated", "xbrl": "https://x/q1.xml",
         "broadcast_Date": "10-Jul-2025 06:40:11"}]
    refs = annual_filing_refs(legacy, integrated)
    urls = [r.url for r in refs]
    assert "https://x/q1.xml" not in urls and "https://x/-" not in urls
    assert "https://x/revised.xml" in urls and "https://x/old.xml" not in urls
    assert urls[0] == "https://x/int25.xml"


def test_choose_basis():
    refs = [FilingRef(date(2026, 3, 31), "Consolidated", "a", None, "integrated"),
            FilingRef(date(2026, 3, 31), "Standalone", "b", None, "integrated")]
    assert choose_basis(refs) == "Consolidated"
    assert choose_basis(refs, prefer="Standalone") == "Standalone"
    assert choose_basis(refs[1:]) == "Standalone"
    assert choose_basis([]) is None

# ------------------------------------------------------------------ corporate actions


@pytest.mark.parametrize("subject,mult", [
    ("Bonus 4:1", 5), ("Bonus 1:1", 2),
    ("Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share", 2),
    (" Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 10),
    ("Face Value Split From Rs.10/- To Rs.2/-", 5),
    ("Dividend - Rs 10 Per Share", None), ("Annual General Meeting", None)])
def test_share_multiplier(subject, mult):
    assert share_multiplier(subject) == mult


def test_adjust_per_share_restates_only_periods_before_ex_date():
    actions = parse_corporate_actions([
        {"exDate": "16-Jun-2025", "subject": "Bonus 4:1"},
        {"exDate": "16-Jun-2025", "subject": "Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share"},
        {"exDate": "16-Jun-2025", "subject": "Bonus 4:1"},          # duplicate row
        {"exDate": "01-Jan-2020", "subject": "Dividend - Rs 5"}])
    assert [a.multiplier for a in actions] == [5, 2]
    facts = [FinancialFact(metric="diluted_eps", period_end=date(2025, 3, 31), value=268.2),
             FinancialFact(metric="diluted_eps", period_end=date(2026, 3, 31), value=30.51),
             FinancialFact(metric="shares_outstanding", period_end=date(2025, 3, 31), value=6.2e8, unit="shares"),
             FinancialFact(metric="revenue", period_end=date(2025, 3, 31), value=100)]
    out, notes = adjust_per_share(facts, actions)
    by = {(f.metric, f.period_end): f for f in out}
    assert by[("diluted_eps", date(2025, 3, 31))].value == pytest.approx(26.82)
    assert by[("diluted_eps", date(2025, 3, 31))].status == DataStatus.DERIVED
    assert by[("diluted_eps_reported", date(2025, 3, 31))].value == 268.2   # as-filed kept
    assert by[("diluted_eps", date(2026, 3, 31))].value == 30.51            # after ex-date: unchanged
    assert by[("shares_outstanding", date(2025, 3, 31))].value == pytest.approx(6.2e9)
    assert by[("revenue", date(2025, 3, 31))].value == 100
    assert len(notes) == 2

# ------------------------------------------------------------------ ownership

SHP = [{"date": "30-SEP-2026", "pr_and_prgrp": "54.67", "public_val": "45.3", "xbrl": "https://x/shp.xml",
        "broadcastDate": "19-OCT-2026 18:37:52"},
       {"date": "30-JUN-2026", "pr_and_prgrp": "55.00", "public_val": "45.0", "xbrl": "https://x/shp0.xml",
        "broadcastDate": "19-JUL-2026 18:37:52"}]


def test_parse_ownership_with_verified_pledge():
    pledge = {"data": [{"shp": "30-Sep-2026", "totIssuedShares": "6853458827", "totPromoterHolding": "1644830118",
                        "totPromoterShares": "1200509465", "percPromoterShares": "72.99", "percTotShares": "17.52"}]}
    snap, warns, url = parse_ownership(SHP, pledge)
    assert snap.as_of == date(2026, 9, 30) and snap.promoter_holding_pct == 54.67
    assert snap.promoter_pledged_pct_of_promoter == 72.99 and snap.promoter_pledged_pct_of_total == 17.52
    assert [p[1] for p in snap.promoter_history] == [54.67, 55.0]
    assert url == "https://x/shp.xml" and warns == []


def test_parse_ownership_rejects_inconsistent_pledge_and_handles_no_promoter():
    bad = {"data": [{"shp": "30-Sep-2026", "totIssuedShares": "1000", "totPromoterHolding": "500",
                     "totPromoterShares": "100", "percPromoterShares": "90.00", "percTotShares": "10.00"}]}
    snap, warns, _ = parse_ownership(SHP, bad)
    assert snap.promoter_pledged_pct_of_promoter is None and any("inconsistent" in w for w in warns)
    no_promoter = [{**SHP[0], "pr_and_prgrp": "0"}]
    snap, warns, _ = parse_ownership(no_promoter, {"data": [{"shp": "30-Sep-2026", "totPromoterHolding": "0",
                                                              "totPromoterShares": "0", "percPromoterShares": "0"}]})
    assert snap.promoter_pledged_pct_of_promoter is None and any("not applicable" in w for w in warns)
    assert parse_ownership([], {})[0] is None

# ------------------------------------------------------------------ client & provider


class FakeResponse:
    def __init__(self, status, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content or text.encode()


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return FakeResponse(404)


def test_client_caches_documents_and_listings(tmp_path):
    sess = FakeSession({"doc.xml": FakeResponse(200, content=b"<x/>"), "api/list": FakeResponse(200, '{"a": 1}')})
    c = NSEClient(tmp_path, min_interval=0, session=sess)
    assert c.get_document("https://h/doc.xml") == b"<x/>"
    assert c.get_document("https://h/doc.xml") == b"<x/>"
    assert c.get_json("https://h/api/list") == {"a": 1}
    assert c.get_json("https://h/api/list") == {"a": 1}
    assert len(sess.calls) == 2
    with pytest.raises(ProviderUnavailable, match="403"):
        NSEClient(tmp_path, min_interval=0, session=FakeSession({"x": FakeResponse(403)})).get_json("https://h/x")


def test_provider_end_to_end_with_fake_nse(tmp_path):
    import json
    routes = {
        "corporates-financial-results": FakeResponse(200, "[]"),
        "integrated-filing-results": FakeResponse(200, json.dumps({"data": [
            {"qe_Date": "31-MAR-2026", "consolidated": "Consolidated", "xbrl": "https://h/INTEGRATED_FILING_INDAS_9.xml",
             "broadcast_Date": "09-Apr-2026 23:42:18"}]})),
        "INTEGRATED_FILING_INDAS_9.xml": FakeResponse(200, content=INDUSTRIAL_FY26),
        "corporates-corporateActions": FakeResponse(200, "[]"),
        "corporate-share-holdings-master": FakeResponse(200, json.dumps(SHP)),
        "corporate-pledgedata": FakeResponse(200, json.dumps({"data": []})),
    }
    provider = NSEFilingsProvider(NSEClient(tmp_path, min_interval=0, session=FakeSession(routes)))
    b = provider.fetch(SecurityIdentity(company_name="ABC Limited", nse_symbol="ABC"))
    assert b.company_type == "industrial" and b.statement_basis == "Consolidated"
    rev = next(f for f in b.facts if f.metric == "revenue")
    src = next(s for s in b.sources if s.source_id == rev.source_id)
    assert src.kind.value == "company_filing" and src.url == "https://h/INTEGRATED_FILING_INDAS_9.xml"
    assert b.ownership.promoter_holding_pct == 54.67
    assert b.audit.auditors == ["Fictional & Co LLP"] and b.audit.opinion_unmodified is True
    with pytest.raises(ProviderUnavailable):
        provider.fetch(SecurityIdentity(company_name="BSE only", bse_code="500001"))


def test_provider_reports_unavailable_when_nse_blocks(tmp_path):
    provider = NSEFilingsProvider(NSEClient(tmp_path, min_interval=0, session=FakeSession({})))
    with pytest.raises(ProviderUnavailable, match="no usable filings"):
        provider.fetch(SecurityIdentity(company_name="ABC Limited", nse_symbol="ABC"))

# ------------------------------------------------------------------ reconciliation & merge


def test_reconcile():
    d = date(2025, 3, 31)
    primary = [FinancialFact(metric="revenue", period_end=d, value=1000), FinancialFact(metric="net_income", period_end=d, value=100)]
    close = [FinancialFact(metric="revenue", period_end=d, value=1010), FinancialFact(metric="net_income", period_end=d, value=101)]
    far = [FinancialFact(metric="revenue", period_end=d, value=1200)]
    usd = [FinancialFact(metric="revenue", period_end=d, value=12, unit="USD")]
    assert reconcile(primary, close, "Y")[0].severity == "info"
    assert reconcile(primary, far, "Y")[0].severity == "warning"
    assert "different currency" in reconcile(primary, usd, "Y")[0].message
    assert reconcile(primary, [], "Y") == []


def test_research_merge_prefers_filings_and_takes_price_from_yahoo(alpha_identity, nse_df, bse_df, facts,
                                                                    market, profile, fin_source):
    from eii.models import OwnershipSnapshot, Source, SourceKind
    from eii.providers.registry import ProviderRegistry
    from eii.services import build_report
    from test_identity_search_providers import FakeFundamentals

    filing_src = Source(source_id="filing", title="filing", provider="NSE", kind=SourceKind.COMPANY_FILING)
    filed = [f.model_copy(update={"source_id": "filing"}) for f in facts]
    nse_bundle = FundamentalsBundle(facts=filed, sources=[filing_src], company_type="industrial",
                                    statement_basis="Consolidated",
                                    ownership=OwnershipSnapshot(as_of=date(2026, 9, 30), promoter_holding_pct=60,
                                                                promoter_pledged_pct_of_promoter=10))
    yahoo_facts = [f.model_copy(update={"source_id": "yfin", "value": f.value * 1.2}) for f in facts]
    yahoo_bundle = FundamentalsBundle(profile=profile, market=market, facts=yahoo_facts, sources=[
        fin_source, Source(source_id="yfin", title="yahoo fin", provider="Y", kind=SourceKind.UNOFFICIAL_AGGREGATOR)])

    class NSEFake(FakeFundamentals):
        name, kind = "NSE fake", SourceKind.COMPANY_FILING

    reg = ProviderRegistry(nse=None, bse=None, fundamentals=[NSEFake(nse_bundle), FakeFundamentals(yahoo_bundle)])
    r = build_report(alpha_identity, reg, nse_df, bse_df, []).report
    assert all(f.source_id == "filing" for f in r.financials)          # one statement source only
    assert "yfin" not in {s.source_id for s in r.sources}              # unused provider statements not cited
    assert r.market.price == market.price and r.profile is not None    # price/profile from Yahoo
    assert r.ownership.promoter_pledged_pct_of_promoter == 10
    assert any(w.severity == "warning" and "differ" in w.message for w in r.warnings)  # 20% gap flagged
    assert r.company_type == "industrial"


def test_key_metrics_bank_and_filed_governance():
    from eii.calculations import facts_to_frame, key_metrics
    from eii.models import AuditInfo, GovernanceInputs, OwnershipSnapshot
    rows = {date(2024, 3, 31): dict(net_income=100, total_assets=5000, total_equity=600, net_interest_income=150,
                                    operating_expenses=80, other_income=50, provisions=20, advances=3000,
                                    gnpa_pct=0.02, cet1_ratio=0.15, revenue=400),
            date(2025, 3, 31): dict(net_income=120, total_assets=6000, total_equity=700, net_interest_income=180,
                                    operating_expenses=90, other_income=60, provisions=24, advances=3600,
                                    gnpa_pct=0.018, cet1_ratio=0.16, revenue=480)}
    facts = [FinancialFact(metric=m, period_end=d, value=v, unit="ratio" if m.endswith(("pct", "ratio")) else "INR")
             for d, r in rows.items() for m, v in r.items()]
    km = key_metrics(facts_to_frame(facts), None, GovernanceInputs(promoter_pledge_pct=99),
                     ownership=OwnershipSnapshot(as_of=date(2026, 9, 30), promoter_pledged_pct_of_promoter=5),
                     audit=AuditInfo(period_end=date(2025, 3, 31), opinion="Statement on impact of audit qualification"))
    v = km.values
    assert v["roa_avg_3y"] == pytest.approx((100 / 5000 + 120 / 6000) / 2)
    assert v["nii_to_assets"] == pytest.approx(180 / 6000)
    assert v["cost_to_income"] == pytest.approx(90 / 240)
    assert v["credit_cost"] == pytest.approx(24 / 3600)
    assert v["gnpa_pct"] == 0.018 and v["cet1_ratio"] == 0.16
    assert v["promoter_pledge_pct"] == 5            # filed value beats analyst's 99
    assert v["audit_opinion_clean"] == 0.0          # modified opinion


def test_lender_types_get_no_ev_or_fcf_metrics(facts, market):
    from eii.calculations import facts_to_frame, key_metrics
    ind = key_metrics(facts_to_frame(facts), market, company_type="industrial").values
    bank = key_metrics(facts_to_frame(facts), market, company_type="bank")
    assert ind["ev_ebitda"] is not None and ind["fcf_yield"] is not None
    assert bank.values["ev_ebitda"] is None and bank.values["fcf_yield"] is None
    assert "Not meaningful" in bank.notes["ev_ebitda"]
    assert bank.values["pe"] == pytest.approx(ind["pe"])  # P/E and P/B still apply


def test_latest_filing_type_maps_all_years(tmp_path):
    """NBFCs filed under plain Ind-AS before 2025; older years must use the NBFC mapper too."""
    import json
    old_indas = doc(ctx("FourD", "2023-04-01", "2024-03-31") + ctx("OneI", instant="2024-03-31"), {
        ("InterestEarned", "FourD"): 700, ("FinanceCosts", "FourD"): 300, ("RevenueFromOperations", "FourD"): 800,
        ("ProfitBeforeExceptionalItemsAndTax", "FourD"): 200, ("Loans", "OneI"): 4000})
    new_nbfc = doc(ctx("FourD", "2024-04-01", "2025-03-31") + ctx("OneI", instant="2025-03-31"), {
        ("InterestEarned", "FourD"): 800, ("FinanceCosts", "FourD"): 350, ("RevenueFromOperations", "FourD"): 900,
        ("Loans", "OneI"): 5000})
    routes = {
        "corporates-financial-results": FakeResponse(200, json.dumps([
            {"toDate": "31-Mar-2024", "consolidated": "Consolidated", "xbrl": "https://h/INDAS_1.xml",
             "broadCastDate": "25-Apr-2024 09:14:49"}])),
        "integrated-filing-results": FakeResponse(200, json.dumps({"data": [
            {"qe_Date": "31-MAR-2025", "consolidated": "Consolidated",
             "xbrl": "https://h/INTEGRATED_FILING_NBFC_INDAS_2.xml", "broadcast_Date": "29-Apr-2025 10:47:24"}]})),
        "INDAS_1.xml": FakeResponse(200, content=old_indas),
        "NBFC_INDAS_2.xml": FakeResponse(200, content=new_nbfc),
        "corporates-corporateActions": FakeResponse(200, "[]"),
        "corporate-share-holdings-master": FakeResponse(200, "[]"),
        "corporate-pledgedata": FakeResponse(200, "{}"),
    }
    b = NSEFilingsProvider(NSEClient(tmp_path, min_interval=0, session=FakeSession(routes))).fetch(
        SecurityIdentity(company_name="ABC Finance Limited", nse_symbol="ABCF"))
    assert b.company_type == "nbfc"
    metrics_2024 = {f.metric for f in b.facts if f.period_end == date(2024, 3, 31)}
    assert "net_interest_income" in metrics_2024 and "ebit" not in metrics_2024
