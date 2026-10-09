"""Hold a deletion lease for each orphan file candidate

The orphan sweep acquires one of these before a stored file is touched, and
the upload path locks and revokes the same row when it re-registers the
digest, so re-referencing a file can no longer race its unlink.

Revision ID: cea5b5e75cbe
Revises: d7cd57abc8a4
Create Date: 2026-10-09 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cea5b5e75cbe"
down_revision: str | Sequence[str] | None = "d7cd57abc8a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "storage_delete_leases",
        sa.Column("digest", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("retry_after", sa.DateTime(), nullable=True),
        sa.Column(
            "created", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False
        ),
        sa.Column(
            "updated", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("digest"),
    )
    with op.batch_alter_table("storage_delete_leases", schema=None) as batch_op:
        batch_op.create_index(
            "ix_storage_delete_leases_due", ["status", "available_at"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("storage_delete_leases", schema=None) as batch_op:
        batch_op.drop_index("ix_storage_delete_leases_due")

    op.drop_table("storage_delete_leases")
