#!/usr/bin/env python
"""Run the complete Phase 1 pipeline offline against deterministic fixture data."""

from datetime import date
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price
from alpha_lab.factors import calculate_factors
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers import SyntheticFixtureProvider
from alpha_lab.strategy import composite_score


def run() -> None:
    evaluation_date = date(2024, 6, 30)
    with tempfile.TemporaryDirectory(prefix="alphalab-smoke-") as directory:
        engine = make_engine(f"sqlite:///{directory}/smoke.db")
        create_schema(engine)
        IngestionService(SyntheticFixtureProvider(), engine).ingest(
            "FIXTURE", date(2023, 1, 1), evaluation_date
        )
        with Session(engine) as session:
            prices = session.scalars(select(Price).order_by(Price.date)).all()
            fundamentals = session.scalars(select(Fundamental).order_by(Fundamental.period)).all()
        price_series = pd.Series({row.date: row.adjusted_close for row in prices})
        frame = pd.DataFrame([{name: getattr(row, name) for name in
            ["period", "publication_date", "revenue", "ebitda", "net_income", "eps", "free_cash_flow",
             "total_debt", "cash", "total_equity"]} for row in fundamentals])
        factors = calculate_factors(price_series, frame, evaluation_date)
        settings = load_settings()
        categories = {name: None for name in settings.weights}
        categories.update(earnings=75.0, fundamentals=70.0, momentum=65.0, balance_sheet=80.0)
        score = composite_score(categories, settings.weights, evaluation_date)
        assert len(prices) >= 250, "fixture must support long-horizon momentum"
        assert factors["return_12m"] > 0
        assert factors["eps_yoy_growth"] > 0
        assert score.score is not None and score.coverage > 0
        print(f"AlphaLab offline smoke test passed: score={score.score}, config={score.config_hash[:12]}")


if __name__ == "__main__":
    run()
