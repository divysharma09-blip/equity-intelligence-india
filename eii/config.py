"""Application settings and logging.

All tunables come from environment variables (optionally loaded from `.env`).
Nothing secret is required for Phase 1; API keys for future providers must be
read here from the environment and never hardcoded.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent

APP_NAME = "Equity Intelligence India"
FRAMEWORK_DISCLAIMER = (
    "The score is a transparent research framework that summarises the data shown. "
    "It is not a scientifically validated prediction of returns and is not investment advice."
)

NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


class Settings(BaseModel):
    data_dir: Path
    db_path: Path
    excel_path: Path
    reference_dir: Path
    backup_dir: Path
    log_dir: Path
    log_level: str = "INFO"
    enable_yfinance: bool = True
    enable_nse_filings: bool = True
    nse_min_interval_seconds: float = Field(default=1.0, ge=0.2)
    nse_listing_ttl_hours: float = Field(default=12, ge=0)
    nse_master_url: str = NSE_EQUITY_LIST_URL
    reference_cache_hours: int = Field(default=24, ge=1)
    http_timeout_seconds: int = Field(default=20, ge=1)
    max_backups: int = Field(default=20, ge=1)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.reference_dir, self.backup_dir, self.log_dir,
                  self.db_path.parent, self.excel_path.parent):
            d.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Path | None = None) -> Settings:
    """Build settings from the environment. `env_file` defaults to PROJECT_ROOT/.env."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    data_dir = _env_path("EII_DATA_DIR", PROJECT_ROOT / "data")
    return Settings(
        data_dir=data_dir,
        db_path=_env_path("EII_DB_PATH", data_dir / "equity_research.db"),
        excel_path=_env_path("EII_EXCEL_PATH", data_dir / "Equity_Intelligence_India.xlsx"),
        reference_dir=_env_path("EII_REFERENCE_DIR", data_dir / "reference"),
        backup_dir=_env_path("EII_BACKUP_DIR", data_dir / "backups"),
        log_dir=_env_path("EII_LOG_DIR", PROJECT_ROOT / "logs"),
        log_level=os.getenv("EII_LOG_LEVEL", "INFO").upper(),
        enable_yfinance=_env_bool("EII_ENABLE_YFINANCE", True),
        enable_nse_filings=_env_bool("EII_ENABLE_NSE_FILINGS", True),
        nse_min_interval_seconds=float(os.getenv("EII_NSE_MIN_INTERVAL_SECONDS", "1.0")),
        nse_listing_ttl_hours=float(os.getenv("EII_NSE_LISTING_TTL_HOURS", "12")),
        nse_master_url=os.getenv("EII_NSE_MASTER_URL", NSE_EQUITY_LIST_URL),
        reference_cache_hours=int(os.getenv("EII_REFERENCE_CACHE_HOURS", "24")),
        http_timeout_seconds=int(os.getenv("EII_HTTP_TIMEOUT_SECONDS", "20")),
        max_backups=int(os.getenv("EII_MAX_BACKUPS", "20")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = load_settings()
    settings.ensure_dirs()
    return settings


_LOGGING_CONFIGURED = False


def setup_logging(settings: Settings) -> None:
    """Console + rotating file logging. Idempotent (Streamlit reruns the script)."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger("eii")
    root.setLevel(settings.log_level)
    file_handler = RotatingFileHandler(settings.log_dir / "eii.log", maxBytes=2_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(console)
    root.propagate = False
    _LOGGING_CONFIGURED = True
