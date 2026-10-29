from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.db import session as db_session
from src.db.models import EtlRun


@pytest.mark.parametrize("raise_error", [False, True])
def test_request_session_rolls_back_uncommitted_work(
    migrated_db, monkeypatch, raise_error
):
    engine, _ = migrated_db
    monkeypatch.setattr(db_session, "get_session_factory", lambda: sessionmaker(engine))
    dependency = db_session.get_db()
    session = next(dependency)
    run = EtlRun(
        run_type="manual", date_from=date(2026, 10, 19), date_to=date(2026, 10, 19)
    )
    session.add(run)
    session.flush()
    run_id = run.id
    if raise_error:
        with pytest.raises(RuntimeError, match="request failed"):
            dependency.throw(RuntimeError("request failed"))
    else:
        with pytest.raises(StopIteration):
            next(dependency)
    with engine.connect() as connection:
        assert connection.scalar(select(EtlRun.id).where(EtlRun.id == run_id)) is None
