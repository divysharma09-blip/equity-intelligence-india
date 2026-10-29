import sqlite3

import pytest

from eii.db import ResearchRepository, backup_db, init_db
from eii.db.database import transaction
from eii.models import ResearchReport, Risk, ScenarioAssumption, SecurityIdentity
from eii.services.research import refresh_analysis


@pytest.fixture
def report(alpha_identity, facts, market, profile, fin_source):
    r = ResearchReport(identity=alpha_identity, financials=facts, market=market, profile=profile,
                       sources=[fin_source], business_model="Makes fictional chemicals.",
                       risks=[Risk(category="Customer concentration", description="Top client 30%",
                                   severity="high")],
                       scenarios=[ScenarioAssumption(scenario="base", horizon_years=5, eps_growth=0.1, exit_pe=20)])
    return refresh_analysis(r)


def test_init_db_is_idempotent(settings):
    init_db(settings.db_path)
    init_db(settings.db_path)
    conn = sqlite3.connect(settings.db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"companies", "research_reports", "financial_facts", "sources", "risks", "scenarios",
            "factor_scores", "research_history", "app_settings"} <= tables


def test_report_persists_across_restart(settings, repo, report):
    saved = repo.save_report(report)
    assert saved.version == 1
    # Simulate an application restart: brand-new repository object on the same file.
    init_db(settings.db_path)
    fresh = ResearchRepository(settings.db_path)
    loaded = fresh.get_report(saved.report_id)
    assert loaded is not None
    assert loaded.identity.isin == report.identity.isin
    assert loaded.status == "completed"
    assert len(loaded.financials) == len(report.financials)
    assert loaded.risks[0].description == "Top client 30%"
    assert loaded.scorecard.overall_score == report.scorecard.overall_score
    companies = fresh.list_companies()
    assert len(companies) == 1 and companies.iloc[0]["company_name"] == "Alpha Industries Limited"


def test_versions_append_and_preserve_history(repo, report):
    first = repo.save_report(report)
    edited = report.model_copy(update={"business_model": "Updated thesis."})
    second = repo.save_report(edited)
    assert (first.version, second.version) == (1, 2)
    assert first.report_id != second.report_id
    assert repo.get_report(first.report_id).business_model == "Makes fictional chemicals."
    assert repo.get_report(second.report_id).business_model == "Updated thesis."
    versions = repo.list_reports(first.company_id)
    assert list(versions["version"]) == [2, 1]
    assert repo.list_companies().iloc[0]["versions"] == 2
    actions = list(repo.history()["action"])
    assert actions.count("saved_report") == 2 and "company_added" in actions


def test_child_tables_written(settings, repo, report):
    saved = repo.save_report(report)
    conn = sqlite3.connect(settings.db_path)
    count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE report_id=?", (saved.report_id,)).fetchone()[0]
    assert count("financial_facts") == len(report.financials)
    assert count("risks") == 1 and count("scenarios") == 1 and count("sources") == 1
    assert count("factor_scores") == len(report.scorecard.factors)
    conn.close()


def test_company_codes_not_erased_by_later_report(repo, report):
    repo.save_report(report)
    no_bse = report.model_copy(update={"identity": report.identity.model_copy(update={"bse_code": None})})
    repo.save_report(no_bse)
    assert repo.list_companies().iloc[0]["bse_code"] == "599001"


def test_failed_transaction_rolls_back(settings, repo):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(settings.db_path) as conn:
            conn.execute("INSERT INTO companies(company_key, company_name, verification_status, created_at,"
                         " updated_at) VALUES ('K','n','verified','t','t')")
            conn.execute("INSERT INTO companies(company_key, company_name, verification_status, created_at,"
                         " updated_at) VALUES ('K','dup','verified','t','t')")
    assert repo.list_companies().empty


def test_excel_sync_flags(repo, report):
    saved = repo.save_report(report)
    assert repo.unsynced_reports() == [saved.report_id]
    repo.mark_excel_synced(saved.report_id, error="locked")
    assert repo.list_reports(saved.company_id).iloc[0]["excel_error"] == "locked"
    repo.mark_excel_synced(saved.report_id)
    assert repo.unsynced_reports() == []


def test_settings_persist(settings, repo):
    repo.set_setting("weights", {"growth": 30})
    repo.set_setting("weights", {"growth": 25})
    assert ResearchRepository(settings.db_path).get_setting("weights") == {"growth": 25}
    assert repo.get_setting("missing", "dflt") == "dflt"


def test_backup_db(settings, repo, report):
    repo.save_report(report)
    path = backup_db(settings.db_path, settings.backup_dir, max_backups=2)
    assert path.exists()
    assert len(ResearchRepository(path).list_companies()) == 1


def test_identity_without_codes_cannot_be_saved(repo):
    bad = ResearchReport(identity=SecurityIdentity(company_name="Nameless"))
    with pytest.raises(ValueError):
        repo.save_report(bad)
