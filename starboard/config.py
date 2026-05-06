"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    secret_key: str
    admin_email: str
    database_path: str
    enable_user_submissions: bool
    week_timezone: str
    submission_lookback_days: int

    @classmethod
    def from_env(cls) -> "Config":
        secret = os.environ.get("SECRET_KEY")
        if not secret:
            raise RuntimeError(
                "SECRET_KEY must be set. Generate one with: "
                "python -c 'import secrets; print(secrets.token_hex(32))'"
            )
        return cls(
            secret_key=secret,
            admin_email=os.environ.get("ADMIN_EMAIL", "").lower().strip(),
            database_path=os.environ.get("DATABASE_PATH", "starboard.db"),
            enable_user_submissions=os.environ.get(
                "ENABLE_USER_SUBMISSIONS", "false"
            ).lower()
            in ("1", "true", "yes", "on"),
            week_timezone=os.environ.get("WEEK_TIMEZONE", "America/Chicago"),
            submission_lookback_days=int(
                os.environ.get("SUBMISSION_LOOKBACK_DAYS", "7")
            ),
        )
