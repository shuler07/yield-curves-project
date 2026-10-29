"""Load connection settings without connecting at import time."""

import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_database_url() -> URL:
    # Read a fixed project file, regardless of the process working directory.
    # Environment variables always take precedence; do not mutate os.environ.
    value = os.environ.get("DATABASE_URL")
    if value is None:
        value = dotenv_values(PROJECT_ROOT / ".env").get("DATABASE_URL")
    if not value:
        raise ValueError("Set DATABASE_URL in the environment or the project .env file")
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError(
            "DATABASE_URL must be a valid PostgreSQL connection URL"
        ) from None
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("DATABASE_URL must use postgresql+psycopg://")
    return url.set(drivername="postgresql+psycopg")
