-- Equity Intelligence India — SQLite schema (version 1)
-- Research is append-only: every save creates a new report version; nothing is
-- overwritten or deleted by the application.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    company_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key          TEXT NOT NULL UNIQUE,      -- ISIN, else NSE:<sym>, else BSE:<code>
    company_name         TEXT NOT NULL,
    nse_symbol           TEXT,
    bse_code             TEXT,
    isin                 TEXT,
    sector               TEXT,
    industry             TEXT,
    verification_status  TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_reports (
    report_id            TEXT PRIMARY KEY,
    company_id           INTEGER NOT NULL REFERENCES companies(company_id),
    version              INTEGER NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('draft', 'completed')),
    verification_status  TEXT NOT NULL,
    overall_score        REAL,
    score_coverage       REAL,
    can_conclude         INTEGER NOT NULL DEFAULT 0,
    analyst              TEXT,
    report_json          TEXT NOT NULL,
    excel_synced         INTEGER NOT NULL DEFAULT 0,
    excel_error          TEXT,
    created_at           TEXT NOT NULL,
    UNIQUE (company_id, version)
);

CREATE TABLE IF NOT EXISTS financial_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id    TEXT NOT NULL REFERENCES research_reports(report_id),
    metric       TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    value        REAL,
    unit         TEXT,
    status       TEXT NOT NULL,
    source_id    TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id     TEXT NOT NULL REFERENCES research_reports(report_id),
    source_id     TEXT NOT NULL,
    title         TEXT NOT NULL,
    provider      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    url           TEXT,
    retrieved_at  TEXT NOT NULL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS risks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id    TEXT NOT NULL REFERENCES research_reports(report_id),
    category     TEXT NOT NULL,
    description  TEXT NOT NULL,
    severity     TEXT NOT NULL,
    mitigation   TEXT,
    status       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenarios (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id         TEXT NOT NULL REFERENCES research_reports(report_id),
    scenario          TEXT NOT NULL,
    horizon_years     INTEGER NOT NULL,
    assumptions_json  TEXT NOT NULL,
    target_price      REAL,
    cagr              REAL
);

CREATE TABLE IF NOT EXISTS factor_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id     TEXT NOT NULL REFERENCES research_reports(report_id),
    factor        TEXT NOT NULL,
    score         REAL,
    weight        REAL NOT NULL,
    coverage      REAL NOT NULL,
    details_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL REFERENCES companies(company_id),
    report_id   TEXT REFERENCES research_reports(report_id),
    action      TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_company ON research_reports(company_id, version);
CREATE INDEX IF NOT EXISTS idx_facts_report ON financial_facts(report_id);
CREATE INDEX IF NOT EXISTS idx_history_company ON research_history(company_id);
