"""Saving completed research: SQLite (source of truth) first, then the Excel workbook.

Only called after the user explicitly confirms. If the user declines, nothing
here runs and neither store is modified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from eii.db import ResearchRepository, SavedReport
from eii.export import ExcelExportError, WorkbookManager
from eii.models import ResearchReport

log = logging.getLogger("eii.services")


@dataclass
class SaveOutcome:
    saved: SavedReport
    excel_ok: bool
    excel_error: Optional[str] = None


def save_completed_report(report: ResearchReport, repo: ResearchRepository,
                          workbook: WorkbookManager) -> SaveOutcome:
    saved = repo.save_report(report)
    try:
        workbook.append_report(saved.report, saved.version)
    except ExcelExportError as exc:
        repo.mark_excel_synced(saved.report_id, error=str(exc))
        return SaveOutcome(saved, False, str(exc))
    except Exception as exc:  # keep the DB save; surface the Excel problem for retry
        log.exception("Unexpected Excel export failure")
        repo.mark_excel_synced(saved.report_id, error=f"Unexpected error: {exc}")
        return SaveOutcome(saved, False, f"Unexpected error: {exc}")
    repo.mark_excel_synced(saved.report_id)
    return SaveOutcome(saved, True)


def retry_excel_sync(repo: ResearchRepository, workbook: WorkbookManager) -> tuple[int, list[str]]:
    """Push reports whose Excel write previously failed. Returns (synced count, errors)."""
    synced, errors = 0, []
    for report_id in repo.unsynced_reports():
        report, meta = repo.get_report(report_id), repo.get_report_meta(report_id)
        if report is None or meta is None:
            continue
        try:
            workbook.append_report(report, int(meta["version"]))
        except ExcelExportError as exc:
            repo.mark_excel_synced(report_id, error=str(exc))
            errors.append(str(exc))
            break  # same cause will fail the rest
        repo.mark_excel_synced(report_id)
        synced += 1
    return synced, errors
