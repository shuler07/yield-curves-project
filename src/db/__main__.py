"""Connection smoke check: python -m src.db."""

from sqlalchemy import text

from src.db.session import get_engine


def main() -> None:
    engine = get_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        print("PostgreSQL connection OK")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
