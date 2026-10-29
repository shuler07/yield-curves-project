"""Synchronous sessions usable by ETL and FastAPI's Depends(get_db)."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.db.config import get_database_url


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_database_url(), pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """One session per request; the caller explicitly commits its transaction.

    Closing also rolls back uncommitted work, including on request exceptions.
    Use this sync dependency in sync FastAPI handlers (def, not async def).
    """
    with get_session_factory()() as session:
        yield session
