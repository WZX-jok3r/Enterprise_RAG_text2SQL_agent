"""Reject SQL NULL version numbers in managed document identities."""
from alembic import op

revision = "a271c48319b6"
down_revision = "9f31d2c8a604"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("version_identity", "documents", type_="check")
    op.create_check_constraint("version_identity", "documents",
        "(document_key IS NULL AND version IS NULL) OR "
        "(document_key IS NOT NULL AND version IS NOT NULL AND version > 0 AND original_filename IS NOT NULL)")


def downgrade():
    op.drop_constraint("version_identity", "documents", type_="check")
    op.create_check_constraint("version_identity", "documents",
        "(document_key IS NULL AND version IS NULL) OR "
        "(document_key IS NOT NULL AND version > 0 AND original_filename IS NOT NULL)")
