import json
import shutil
import subprocess
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from src.etl.calculations import RawSnapshot, ScenarioParameters, calculate_snapshot
from src.etl.common import TERMS


@pytest.mark.parametrize(
    "base_percent,shock_bps", [(10, 400), (0.01, 400), (-5, 400), (10, 100)]
)
def test_html_illustration_matches_python_calculations(base_percent, shock_bps):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed only for HTML/Python scenario parity")
    path = Path(__file__).resolve().parents[2] / "docs/irrbb/irrbb_scenarios.html"
    html = path.read_text(encoding="utf-8")
    source = html[html.index("    const tenors =") : html.index("    const ctx =")]
    program = """
const fs = require('node:fs'), vm = require('node:vm');
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = vm.runInNewContext(p.source + ';baseCurve.fill(' + p.base + ');JSON.stringify(calculateScenarios(' + p.shock + '))');
process.stdout.write(result);
"""
    result = subprocess.run(
        [node, "-e", program],
        input=json.dumps({"source": source, "base": base_percent, "shock": shock_bps}),
        capture_output=True,
        text=True,
        check=True,
    )
    html_rates = json.loads(result.stdout)
    raw = RawSnapshot(
        date(2026, 10, 30),
        time(18),
        {term: Decimal(str(base_percent)) / 100 for term in TERMS},
    )
    params = ScenarioParameters(
        parallel_bps=shock_bps,
        short_bps=Decimal(shock_bps) * Decimal("1.25"),
        long_bps=Decimal(shock_bps) * Decimal("0.75"),
    )
    calculated = calculate_snapshot(raw, params)
    for curve, key in zip(
        calculated.scenarios,
        (
            "parallelUp",
            "parallelDown",
            "shortUp",
            "shortDown",
            "steepener",
            "flattener",
        ),
        strict=True,
    ):
        assert html_rates[key] == pytest.approx(
            [float(point.stressed_yield * 100) for point in curve.points],
            rel=1e-12,
            abs=1e-12,
        )
