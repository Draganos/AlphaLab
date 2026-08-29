"""Optional document-to-AI research workflow; absence or failure remains missing."""

from collections import defaultdict
import hashlib
import json

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.ai.research import AIResearchProvider, analyze_documents
from alpha_lab.database.models import AIResearchAnalysis, CompanyDocument
from alpha_lab.phase3.repository import Phase3Repository


class AIResearchService:
    def __init__(self, engine: Engine, provider: AIResearchProvider | None):
        self.engine, self.provider = engine, provider

    def ensure_all(self) -> int:
        if self.provider is None:
            return 0
        with Session(self.engine) as session:
            documents = list(
                session.scalars(
                    select(CompanyDocument).order_by(CompanyDocument.document_date)
                )
            )
            latest = {}
            for row in session.scalars(
                select(AIResearchAnalysis).order_by(
                    AIResearchAnalysis.analysis_date.desc()
                )
            ):
                latest.setdefault(row.ticker, row)
        grouped: dict[str, list[CompanyDocument]] = defaultdict(list)
        for document in documents:
            grouped[document.ticker].append(document)
        stored = 0
        for ticker, rows in grouped.items():
            input_fingerprint = _document_fingerprint(rows)
            if (
                ticker in latest
                and latest[ticker].raw_output.get("input_document_fingerprint")
                == input_fingerprint
            ):
                continue
            result = analyze_documents(
                self.provider,
                ticker,
                [
                    {
                        "id": row.id,
                        "text": row.text,
                        "title": row.title,
                        "source": row.source,
                        "document_date": row.document_date.isoformat(),
                    }
                    for row in rows
                ],
            )
            if result is not None:
                Phase3Repository(self.engine).save_ai(
                    ticker, result, input_document_fingerprint=input_fingerprint
                )
                stored += 1
        return stored


def _document_fingerprint(rows: list[CompanyDocument]) -> str:
    payload = [
        {
            "id": row.id,
            "date": row.document_date.isoformat(),
            "title": row.title,
            "text": row.text,
            "source": row.source,
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
