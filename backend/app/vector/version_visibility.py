"""PostgreSQL is authoritative; staged/historical vector payloads never self-publish."""
from functools import lru_cache

from sqlalchemy import create_engine, select

from app.core.config import settings
from app.core.performance import timed_stage
from app.models.document import Document


@lru_cache(maxsize=1)
def registry_engine():
    return create_engine(settings.sync_postgres_url, pool_pre_ping=True,
                         pool_size=2, max_overflow=3, connect_args={"connect_timeout": 3})


@timed_stage("version_registry")
def current_revision_ids():
    try:
        with registry_engine().connect() as conn:
            return [str(row[0]) for row in conn.execute(select(Document.id).where(
                Document.document_key.is_not(None), Document.is_current.is_(True), Document.status == "ready"))]
    except Exception as exc:
        # Fail closed instead of returning historical/staged versions during registry outages.
        raise RuntimeError("Document version registry unavailable; retrieval stopped") from exc
