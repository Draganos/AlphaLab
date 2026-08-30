#!/usr/bin/env python
"""Download real provider data for configured US securities."""
from datetime import date, timedelta
from pathlib import Path
import argparse
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers import YFinanceProvider
from alpha_lab.utils.logging import configure_logging

parser = argparse.ArgumentParser()
parser.add_argument("tickers", nargs="*", help="US tickers; defaults to configured universe")
parser.add_argument("--years", type=int, default=5)
args = parser.parse_args()
configure_logging()
settings = load_settings()
engine = make_engine(settings.database_url)
create_schema(engine)
service = IngestionService(YFinanceProvider(), engine)
for ticker in args.tickers or settings.universe["us"]:
    service.ingest(ticker, date.today() - timedelta(days=365 * args.years), date.today())
