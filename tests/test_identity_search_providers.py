import pandas as pd
import pytest
import requests

from conftest import ALPHA_ISIN, BETA_ISIN, NSE_CSV
from eii.models import CompanyProfile, SecurityIdentity, VerificationStatus
from eii.providers.base import FundamentalsBundle, FundamentalsProvider, ProviderStatus, ProviderUnavailable
from eii.providers.exchange_masters import NSEEquityMaster
from eii.providers.registry import ProviderRegistry
from eii.providers.yahoo import INCOME_MAP, map_statement, yahoo_ticker
from eii.services import build_report, classify_query, search_securities
from eii.verification import isin_is_valid, name_similarity, normalize_name, verify_identity

# ------------------------------------------------------------------ ISIN & names


@pytest.mark.parametrize("isin", ["INE467B01029", "INE002A01018", "INE009A01021", "INE040A01034"])
def test_real_isins_pass_check_digit(isin):
    assert isin_is_valid(isin)


@pytest.mark.parametrize("isin", ["INE467B01028", "INE467B0102", "ine467b01029x", None, ""])
def test_invalid_isins(isin):
    assert not isin_is_valid(isin)


def test_name_normalisation():
    assert normalize_name("Tata Consultancy Services Ltd.") == normalize_name("TATA CONSULTANCY SERVICES LIMITED")
    assert name_similarity("M&M Financial Ltd", "M and M Financial Limited") == 1.0
    assert name_similarity("Alpha Industries Limited", "Beta Finance Limited") < 0.6

# ------------------------------------------------------------------ verification


def test_verified_when_exchange_and_provider_agree(alpha_identity, nse_df, bse_df, profile):
    v = verify_identity(alpha_identity, nse_df, bse_df, profile, "ALPHAIND.NS")
    assert v.status == VerificationStatus.VERIFIED, v.checks


def test_name_match_alone_is_not_verification(nse_df):
    # Right name, but a symbol/ISIN pair that the exchange list does not support.
    fake = SecurityIdentity(company_name="Alpha Industries Limited", nse_symbol="ALPHAIND", isin=BETA_ISIN)
    assert verify_identity(fake, nse_df).status == VerificationStatus.CONFLICT
    unknown = SecurityIdentity(company_name="Alpha Industries Limited", nse_symbol="NOTLISTED", isin=ALPHA_ISIN)
    assert verify_identity(unknown, nse_df).status == VerificationStatus.UNVERIFIED


def test_provider_name_mismatch_is_conflict(alpha_identity, nse_df, bse_df):
    wrong = CompanyProfile(provider_name="Completely Different Motors Ltd", provider_symbol="ALPHAIND.NS")
    v = verify_identity(alpha_identity, nse_df, bse_df, wrong, "ALPHAIND.NS")
    assert v.status == VerificationStatus.CONFLICT


def test_partial_when_bse_list_missing_and_provider_unclear(alpha_identity, nse_df):
    unclear = CompanyProfile(provider_name="Alpha Inds Chem", provider_symbol="ALPHAIND.NS")
    v = verify_identity(alpha_identity, nse_df, None, unclear, "ALPHAIND.NS")
    assert v.status == VerificationStatus.PARTIAL
    assert any(c.passed is None and "BSE" in c.name for c in v.checks)

# ------------------------------------------------------------------ search


def test_classify_query():
    assert classify_query(ALPHA_ISIN.lower()) == "isin"
    assert classify_query("500325") == "bse_code"
    assert classify_query("reliance") == "text"


def test_search_by_symbol_isin_name_and_bse_code(nse_df, bse_df):
    assert search_securities("alphaind", nse_df, bse_df)[0].matched_on == "nse_symbol"
    by_isin = search_securities(ALPHA_ISIN, nse_df, bse_df)
    assert by_isin[0].nse_symbol == "ALPHAIND" and by_isin[0].bse_code == "599001"
    by_name = search_securities("alpha", nse_df, bse_df)
    assert {r.nse_symbol for r in by_name} == {"ALPHAIND", "ALPHATEX"}
    by_bse = search_securities("599002", nse_df, bse_df)
    assert by_bse[0].nse_symbol == "BETAFIN" and by_bse[0].matched_on == "bse_code"
    assert search_securities("599002", nse_df, None) == []  # no BSE list -> no guess
    assert search_securities("zzzz-nothing", nse_df, bse_df) == []


def test_bse_parser_drops_delisted(bse_df):
    assert "599099" not in set(bse_df["symbol"])

# ------------------------------------------------------------------ providers


def test_nse_parse(nse_df):
    assert list(nse_df.columns) == ["company_name", "symbol", "isin", "series", "listing_date", "face_value"]
    row = nse_df[nse_df["symbol"] == "ALPHAIND"].iloc[0]
    assert row["isin"] == ALPHA_ISIN and str(row["listing_date"]) == "2008-10-06"


def test_nse_master_falls_back_to_cache_when_offline(tmp_path, monkeypatch):
    provider = NSEEquityMaster("https://example.invalid/EQUITY_L.csv", tmp_path, cache_hours=1)

    def boom(*a, **k):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(ProviderUnavailable, match="offline"):
        provider.load()  # no cache yet -> clear error, no invented data
    provider.cache_path.write_text(NSE_CSV)
    import os
    os.utime(provider.cache_path, (0, 0))  # make the cache stale
    df = provider.load()
    assert len(df) == 3 and "cached copy" in provider.last_warning


def test_yahoo_statement_mapping_skips_nan():
    frame = pd.DataFrame({pd.Timestamp("2025-03-31"): [100.0, float("nan"), 5.0],
                          pd.Timestamp("2024-03-31"): [90.0, float("nan"), float("nan")]},
                         index=["Total Revenue", "EBITDA", "Diluted EPS"])
    facts, used = map_statement(frame, INCOME_MAP, "src")
    assert used == {"revenue": "Total Revenue", "diluted_eps": "Diluted EPS"}
    assert len(facts) == 3  # NaNs are dropped, not zero-filled
    assert next(f for f in facts if f.metric == "diluted_eps").unit == "INR/share"  # default currency INR


def test_yahoo_ticker():
    assert yahoo_ticker(SecurityIdentity(company_name="x", nse_symbol="TCS")) == "TCS.NS"
    assert yahoo_ticker(SecurityIdentity(company_name="x", bse_code="500325")) == "500325.BO"

# ------------------------------------------------------------------ report building


class FakeFundamentals(FundamentalsProvider):
    name = "Fake provider"

    def __init__(self, bundle=None, fail=False):
        self.bundle, self.fail = bundle, fail

    def status(self):
        return ProviderStatus(self.name, True, "ok")

    def fetch(self, identity):
        if self.fail:
            raise ProviderUnavailable("service down")
        return self.bundle


def _registry(provider):
    return ProviderRegistry(nse=None, bse=None, fundamentals=[provider] if provider else [])


def test_build_report_with_provider_data(alpha_identity, nse_df, bse_df, facts, market, profile, fin_source):
    bundle = FundamentalsBundle(profile=profile, market=market, facts=facts, sources=[fin_source])
    res = build_report(alpha_identity, _registry(FakeFundamentals(bundle)), nse_df, bse_df, [])
    r = res.report
    assert r.verification.status == VerificationStatus.VERIFIED
    assert r.scorecard.overall_score is not None
    # governance & macro are analyst inputs -> missing until entered, so coverage < 100%
    assert r.scorecard.coverage < 1
    assert any(w.section == "Governance" for w in r.warnings)


def test_build_report_when_provider_unavailable(alpha_identity, nse_df, bse_df):
    res = build_report(alpha_identity, _registry(FakeFundamentals(fail=True)), nse_df, bse_df, [])
    r = res.report
    assert r.financials == [] and r.market is None
    assert r.scorecard.overall_score is None
    assert not r.readiness.can_conclude
    assert any("service down" in w.message for w in r.warnings)
    assert res.provider_errors


def test_build_report_without_any_provider(alpha_identity, nse_df):
    r = build_report(alpha_identity, _registry(None), nse_df, None, []).report
    assert any("No fundamentals data provider" in w.message for w in r.warnings)
    assert not r.readiness.can_conclude


def test_currency_mismatch_blocks_conclusion(alpha_identity, nse_df, bse_df, facts, market, profile, fin_source):
    from eii.models import ScenarioAssumption
    from eii.services import refresh_analysis
    usd = [f.model_copy(update={"unit": "USD/share" if f.metric == "diluted_eps" else "USD"}) for f in facts]
    bundle = FundamentalsBundle(profile=profile, market=market, facts=usd, sources=[fin_source])
    r = build_report(alpha_identity, _registry(FakeFundamentals(bundle)), nse_df, bse_df, []).report
    assert any(w.severity == "critical" and w.section == "Currency" for w in r.warnings)
    assert not r.readiness.can_conclude
    r = refresh_analysis(r.model_copy(update={"scenarios": [
        ScenarioAssumption(scenario="base", horizon_years=3, eps_growth=0.1, exit_pe=20)]}))
    assert r.scenario_results == []  # no INR price ÷ USD EPS arithmetic


def test_yahoo_eps_unit_follows_currency():
    frame = pd.DataFrame({pd.Timestamp("2025-03-31"): [100.0, 1.5]}, index=["Total Revenue", "Diluted EPS"])
    facts, _ = map_statement(frame, INCOME_MAP, "src", currency="USD")
    units = {f.metric: f.unit for f in facts}
    assert units == {"revenue": "USD", "diluted_eps": "USD/share"}
