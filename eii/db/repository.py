"""Research persistence. All writes are append-only report versions plus an audit trail."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from eii.db.database import connect, transaction
from eii.models import ResearchReport, new_id

log = logging.getLogger("eii.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SavedReport:
    report_id: str
    company_id: int
    company_key: str
    version: int
    report: ResearchReport


class ResearchRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    # ------------------------------------------------------------------ writes

    def save_report(self, report: ResearchReport) -> SavedReport:
        """Persist a completed report as a new version. Previous versions are untouched."""
        saved_at = datetime.now(timezone.utc)
        report = report.model_copy(update={"report_id": new_id(), "status": "completed",
                                           "updated_at": saved_at}, deep=True)
        ident = report.identity
        key = ident.key
        sector = report.profile.sector if report.profile else None
        industry = report.profile.industry if report.profile else None
        vstatus = report.verification.status.value if report.verification else "unverified"
        sc = report.scorecard
        with transaction(self.db_path) as conn:
            company_id = self._upsert_company(conn, report, key, sector, industry, vstatus)
            version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM research_reports WHERE company_id = ?",
                (company_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO research_reports(report_id, company_id, version, status,
                       verification_status, overall_score, score_coverage, can_conclude, analyst,
                       report_json, excel_synced, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
                (report.report_id, company_id, version, report.status, vstatus,
                 sc.overall_score if sc else None, sc.coverage if sc else None,
                 int(bool(report.readiness and report.readiness.can_conclude)), report.analyst,
                 report.model_dump_json(), saved_at.isoformat(timespec="seconds")),
            )
            conn.executemany(
                "INSERT INTO financial_facts(report_id, metric, period_end, value, unit, status, source_id)"
                " VALUES (?,?,?,?,?,?,?)",
                [(report.report_id, f.metric, f.period_end.isoformat(), f.value, f.unit,
                  f.status.value, f.source_id) for f in report.financials],
            )
            conn.executemany(
                "INSERT INTO sources(report_id, source_id, title, provider, kind, url, retrieved_at, notes)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(report.report_id, s.source_id, s.title, s.provider, s.kind.value, s.url,
                  s.retrieved_at.isoformat(), s.notes) for s in report.sources],
            )
            conn.executemany(
                "INSERT INTO risks(report_id, category, description, severity, mitigation, status)"
                " VALUES (?,?,?,?,?,?)",
                [(report.report_id, r.category, r.description, r.severity, r.mitigation,
                  r.status.value) for r in report.risks],
            )
            results = {(r.scenario, r.horizon_years): r for r in report.scenario_results}
            conn.executemany(
                "INSERT INTO scenarios(report_id, scenario, horizon_years, assumptions_json, target_price, cagr)"
                " VALUES (?,?,?,?,?,?)",
                [(report.report_id, a.scenario, a.horizon_years, a.model_dump_json(),
                  getattr(results.get((a.scenario, a.horizon_years)), "target_price", None),
                  getattr(results.get((a.scenario, a.horizon_years)), "cagr", None))
                 for a in report.scenarios],
            )
            if sc:
                conn.executemany(
                    "INSERT INTO factor_scores(report_id, factor, score, weight, coverage, details_json)"
                    " VALUES (?,?,?,?,?,?)",
                    [(report.report_id, f.factor, f.score, f.weight, f.coverage,
                      json.dumps([m.model_dump() for m in f.metrics])) for f in sc.factors],
                )
            self._log(conn, company_id, report.report_id, "saved_report",
                      f"Version {version} saved ({vstatus})")
        log.info("Saved report %s for %s as version %s", report.report_id, key, version)
        return SavedReport(report.report_id, company_id, key, version, report)

    def _upsert_company(self, conn: sqlite3.Connection, report: ResearchReport, key: str,
                        sector: Optional[str], industry: Optional[str], vstatus: str) -> int:
        ident = report.identity
        now = _now()
        row = conn.execute("SELECT company_id FROM companies WHERE company_key = ?", (key,)).fetchone()
        if row:
            # COALESCE keeps previously known codes if this report lacks them.
            conn.execute(
                """UPDATE companies SET company_name=?, nse_symbol=COALESCE(?, nse_symbol),
                       bse_code=COALESCE(?, bse_code), isin=COALESCE(?, isin),
                       sector=COALESCE(?, sector), industry=COALESCE(?, industry),
                       verification_status=?, updated_at=? WHERE company_id=?""",
                (ident.company_name, ident.nse_symbol, ident.bse_code, ident.isin, sector,
                 industry, vstatus, now, row["company_id"]),
            )
            return int(row["company_id"])
        cur = conn.execute(
            """INSERT INTO companies(company_key, company_name, nse_symbol, bse_code, isin, sector,
                   industry, verification_status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (key, ident.company_name, ident.nse_symbol, ident.bse_code, ident.isin, sector,
             industry, vstatus, now, now),
        )
        self._log(conn, cur.lastrowid, None, "company_added", ident.company_name)
        return int(cur.lastrowid)

    @staticmethod
    def _log(conn: sqlite3.Connection, company_id: int, report_id: Optional[str], action: str,
             note: str = "") -> None:
        conn.execute(
            "INSERT INTO research_history(company_id, report_id, action, note, created_at) VALUES (?,?,?,?,?)",
            (company_id, report_id, action, note, _now()),
        )

    def mark_excel_synced(self, report_id: str, error: Optional[str] = None) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE research_reports SET excel_synced=?, excel_error=? WHERE report_id=?",
                         (0 if error else 1, error, report_id))
            row = conn.execute("SELECT company_id FROM research_reports WHERE report_id=?",
                               (report_id,)).fetchone()
            if row:
                self._log(conn, row["company_id"], report_id,
                          "excel_sync_failed" if error else "excel_synced", error or "")

    # ------------------------------------------------------------------ reads

    def list_companies(self) -> pd.DataFrame:
        sql = """
            SELECT c.company_id, c.company_key, c.company_name, c.nse_symbol, c.bse_code, c.isin,
                   c.sector, c.industry, c.verification_status, c.created_at, c.updated_at,
                   r.report_id AS latest_report_id, r.version AS latest_version,
                   r.overall_score, r.score_coverage, r.can_conclude, r.excel_synced,
                   r.created_at AS latest_saved_at,
                   (SELECT COUNT(*) FROM research_reports r2 WHERE r2.company_id = c.company_id) AS versions
            FROM companies c
            LEFT JOIN research_reports r ON r.company_id = c.company_id
                 AND r.version = (SELECT MAX(version) FROM research_reports r3 WHERE r3.company_id = c.company_id)
            ORDER BY c.updated_at DESC"""
        return self._df(sql)

    def list_reports(self, company_id: int) -> pd.DataFrame:
        return self._df(
            """SELECT report_id, version, status, verification_status, overall_score, score_coverage,
                      can_conclude, analyst, excel_synced, excel_error, created_at
               FROM research_reports WHERE company_id=? ORDER BY version DESC""", (company_id,))

    def get_report(self, report_id: str) -> Optional[ResearchReport]:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT report_json FROM research_reports WHERE report_id=?",
                               (report_id,)).fetchone()
        finally:
            conn.close()
        return ResearchReport.model_validate_json(row["report_json"]) if row else None

    def get_report_meta(self, report_id: str) -> Optional[dict[str, Any]]:
        df = self._df("""SELECT r.report_id, r.version, r.company_id, c.company_key
                         FROM research_reports r JOIN companies c USING(company_id)
                         WHERE r.report_id=?""", (report_id,))
        return df.iloc[0].to_dict() if not df.empty else None

    def unsynced_reports(self) -> list[str]:
        df = self._df("SELECT report_id FROM research_reports WHERE excel_synced=0 ORDER BY created_at")
        return df["report_id"].tolist()

    def history(self, company_id: Optional[int] = None) -> pd.DataFrame:
        sql = """SELECT h.created_at, c.company_name, c.company_key, h.action, h.report_id, h.note
                 FROM research_history h JOIN companies c USING(company_id)"""
        params: tuple = ()
        if company_id is not None:
            sql += " WHERE h.company_id=?"
            params = (company_id,)
        return self._df(sql + " ORDER BY h.id DESC", params)

    def _df(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        conn = connect(self.db_path)
        try:
            return pd.read_sql_query(sql, conn, params=params)
        finally:
            conn.close()

    # ------------------------------------------------------------------ settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT value_json FROM app_settings WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO app_settings(key, value_json, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json.dumps(value), _now()),
            )
