"""Configuration, resolved once from the environment (and an optional `.env`).

Every setting has a working default so the system runs with no configuration at
all. Secrets (the MySQL password, the Anthropic key) are only ever read from the
environment — they are never persisted by the application.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Repository root: <root>/src/searchiq/config.py -> <root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def _path(env: str, default: str) -> Path:
    """Resolve a path setting, treating relative values as project-relative."""
    value = Path(os.getenv(env, default))
    return value if value.is_absolute() else PROJECT_ROOT / value


def _int(env: str, default: int) -> int:
    raw = os.getenv(env, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{env} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class MySQLSettings:
    """Connection details for the live source database (optional ingest path)."""

    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class Settings:
    db_path: Path
    dump_path: Path
    sample_dump_path: Path
    mysql: MySQLSettings
    result_cap: int
    session_gap_seconds: int
    anthropic_api_key: str
    model: str

    @property
    def agent_is_live(self) -> bool:
        """True when the agent can call the model rather than fall back locally."""
        return bool(self.anthropic_api_key)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        db_path=_path("SEARCHIQ_DB", "data/searchiq.db"),
        dump_path=_path(
            "SEARCHIQ_DUMP_PATH",
            "C:/Users/Osama/Downloads/grocery_database/spinneys_database.sql",
        ),
        sample_dump_path=_path("SEARCHIQ_SAMPLE_DUMP_PATH", "data/sample_dump.sql"),
        mysql=MySQLSettings(
            host=os.getenv("SEARCHIQ_MYSQL_HOST", "127.0.0.1"),
            port=_int("SEARCHIQ_MYSQL_PORT", 3306),
            user=os.getenv("SEARCHIQ_MYSQL_USER", "root"),
            password=os.getenv("SEARCHIQ_MYSQL_PASSWORD", ""),
            database=os.getenv("SEARCHIQ_MYSQL_DATABASE", "spinneys_database"),
        ),
        result_cap=_int("SEARCHIQ_RESULT_CAP", 5),
        session_gap_seconds=_int("SEARCHIQ_SESSION_GAP_SECONDS", 1800),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        model=os.getenv("SEARCHIQ_MODEL", "claude-opus-5"),
    )
