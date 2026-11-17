"""Settings from .env (exported environment variables take precedence)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_DIR = Path(__file__).resolve().parent.parent

#: The only database this job may write to.
DB_NAME = "powerbidb"
TARGET_TABLE = "product_categorisation_us_ca_uk_it"
DEFAULT_MARKETPLACES = ("US", "CA", "UK", "IT", "DE")


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_user: str
    db_password: str = ""
    db_connect_timeout: int = 30
    #: Marketplaces loaded into the target; anything else in a source (e.g. UAE) is skipped.
    marketplaces: tuple[str, ...] = DEFAULT_MARKETPLACES
    #: CA rows are a copy of the US rows (product_categorisation), as the table has always been built.
    ca_mirrors_us: bool = True
    data_dir: Path = PROJECT_DIR / "data"
    log_dir: Path = PROJECT_DIR / "logs"
    log_level: str = "INFO"
    log_retention_days: int = 30
    backup_retention_days: int = 30

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "sync.lock"

    def __repr__(self) -> str:  # never print the password
        return f"Settings(db={self.db_user}@{self.db_host}:{self.db_port}/{DB_NAME}, marketplaces={self.marketplaces})"


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a whole number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false, got {raw!r}")


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_DIR / p


def load_settings(env_file: Path | None = None) -> Settings:
    """Read .env (or ``env_file``) and validate it."""
    if env_file is not None and not Path(env_file).is_file():
        raise ConfigurationError(f"env file not found: {env_file}")
    load_dotenv(env_file or PROJECT_DIR / ".env", override=False)

    missing = [k for k in ("DB_HOST", "DB_USER", "DB_PASSWORD") if not os.getenv(k, "").strip()]
    if missing:
        raise ConfigurationError(f"missing in .env: {', '.join(missing)}")
    db_name = os.getenv("DB_NAME", DB_NAME).strip()
    if db_name != DB_NAME:
        raise ConfigurationError(f"DB_NAME must be {DB_NAME} (this job only writes {DB_NAME}.{TARGET_TABLE}), got {db_name!r}")

    mps = tuple(m.strip().upper() for m in os.getenv("SYNC_MARKETPLACES", ",".join(DEFAULT_MARKETPLACES)).split(",") if m.strip())
    if not mps:
        raise ConfigurationError("SYNC_MARKETPLACES is empty")

    level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"LOG_LEVEL invalid: {level!r}")

    return Settings(
        db_host=os.environ["DB_HOST"].strip(),
        db_port=_int("DB_PORT", 3306),
        db_user=os.environ["DB_USER"].strip(),
        db_password=os.environ["DB_PASSWORD"],
        db_connect_timeout=_int("DB_CONNECT_TIMEOUT_SECONDS", 30),
        marketplaces=mps,
        ca_mirrors_us=_bool("CA_MIRRORS_US", True),
        data_dir=_path("DATA_DIR", PROJECT_DIR / "data"),
        log_dir=_path("LOG_DIR", PROJECT_DIR / "logs"),
        log_level=level,
        log_retention_days=_int("LOG_RETENTION_DAYS", 30),
        backup_retention_days=_int("BACKUP_RETENTION_DAYS", 30),
    )
