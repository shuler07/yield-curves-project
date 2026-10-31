from pathlib import Path

import pytest


@pytest.fixture
def moex_csv():
    return (Path(__file__).parent / "fixtures" / "moex_range.csv").read_text(
        encoding="utf-8-sig"
    )
