"""Immutable document revisions and a single published version per document."""
from alembic import op
import sqlalchemy as sa

revision = "9f31d2c8a604"
down_revision = "8d4f2a6c1b07"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("document_key", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("version", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("original_filename", sa.String(512), nullable=True))
    op.add_column("documents", sa.Column("is_current", sa.Boolean(), nullable=False, server_default="false"))
    op.create_index("ix_documents_document_key", "documents", ["document_key"])
    op.create_unique_constraint("uq_documents_key_version", "documents", ["document_key", "version"])
    op.create_check_constraint("version_identity", "documents",
        "(document_key IS NULL AND version IS NULL) OR "
        "(document_key IS NOT NULL AND version > 0 AND original_filename IS NOT NULL)")
    op.create_check_constraint("current_ready", "documents",
        "NOT is_current OR (document_key IS NOT NULL AND status = 'ready')")
    op.create_index("uq_documents_current_key", "documents", ["document_key"], unique=True,
                    postgresql_where=sa.text("is_current"))


def downgrade():
    # Refuse to erase version identity/history after versioned ingestion has started.
    if op.get_bind().execute(sa.text("SELECT 1 FROM documents WHERE document_key IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Version history exists; export/migrate it before downgrading")
    op.drop_index("uq_documents_current_key", table_name="documents")
    op.drop_constraint("current_ready", "documents", type_="check")
    op.drop_constraint("version_identity", "documents", type_="check")
    op.drop_constraint("uq_documents_key_version", "documents", type_="unique")
    op.drop_index("ix_documents_document_key", table_name="documents")
    for name in ("is_current", "original_filename", "version", "document_key"):
        op.drop_column("documents", name)
