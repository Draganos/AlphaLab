#!/usr/bin/env python
"""Create or update the local schema (additive SQLAlchemy initialization)."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.utils.logging import configure_logging
import logging

configure_logging()
settings = load_settings()
create_schema(make_engine(settings.database_url))
logging.getLogger(__name__).info("schema_initialized", extra={"database_url": settings.database_url})
