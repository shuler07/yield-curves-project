import math
import os
from dataclasses import dataclass

from dotenv import dotenv_values

from src.db.config import PROJECT_ROOT


@dataclass(frozen=True)
class Settings:
    scheduler_enabled: bool = True
    interval_hours: float = 6
    timeout_seconds: float = 180

    def __post_init__(self):
        for name in ("interval_hours", "timeout_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

    @classmethod
    def from_env(cls):
        values = {**dotenv_values(PROJECT_ROOT / ".env"), **os.environ}
        enabled = str(values.get("ETL_SCHEDULER_ENABLED", "true")).lower()
        if enabled not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("ETL_SCHEDULER_ENABLED must be true or false")
        return cls(
            scheduler_enabled=enabled in {"true", "1", "yes"},
            interval_hours=float(values.get("ETL_INTERVAL_HOURS", "6")),
            timeout_seconds=float(values.get("ETL_TIMEOUT_SECONDS", "180")),
        )
