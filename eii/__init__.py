"""Equity Intelligence India — research-backed Indian stock analysis.

Package layout:
    config        settings, paths, logging
    models        Pydantic domain models (identity, financial facts, reports)
    db            SQLite schema, initialisation and repository
    providers     data-provider interfaces and implementations (no calculations)
    calculations  pure financial-ratio maths on pandas frames
    valuation     scenario, DCF and reverse-DCF maths
    scoring       configurable research-framework scorecard
    verification  company/exchange identity verification
    services      orchestration that wires providers -> calculations -> reports
    export        Excel workbook writer
    ui            Streamlit views
"""

__version__ = "0.1.0"
