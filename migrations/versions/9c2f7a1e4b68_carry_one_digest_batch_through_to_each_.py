"""Carry one digest batch through to each recipient durably

Revision ID: 9c2f7a1e4b68
Revises: d7cd57abc8a4
Create Date: 2026-10-09 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c2f7a1e4b68"
down_revision: str | Sequence[str] | None = "d7cd57abc8a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "group_digests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column(
            "created", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("group_digests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_group_digests_library_id"), ["library_id"])
        batch_op.create_index("ix_group_digests_open", ["completed_at"])

    op.create_table(
        "group_digest_recipients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("digest_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("email_body", sa.String(), nullable=False),
        sa.Column("notify_items_changed", sa.Boolean(), nullable=False),
        sa.Column("notify_items_deleted", sa.Boolean(), nullable=False),
        sa.Column("notify_members_changed", sa.Boolean(), nullable=False),
        sa.Column("notify_collections_changed", sa.Boolean(), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=True),
        sa.Column("email_state", sa.String(length=16), nullable=False),
        sa.Column("email_attempts", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["digest_id"], ["group_digests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("digest_id", "user_id", name="uq_group_digest_recipient"),
        sa.UniqueConstraint("notification_id", name="uq_group_digest_recipient_notice"),
    )
    with op.batch_alter_table("group_digest_recipients", schema=None) as batch_op:
        batch_op.create_index("ix_group_digest_recipients_digest", ["digest_id", "id"])

    with op.batch_alter_table("group_activity", schema=None) as batch_op:
        batch_op.add_column(sa.Column("digest_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_group_activity_digest_id"), ["digest_id"])
        batch_op.create_foreign_key(
            "fk_group_activity_digest_id",
            "group_digests",
            ["digest_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("group_activity", schema=None) as batch_op:
        batch_op.drop_constraint("fk_group_activity_digest_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_group_activity_digest_id"))
        batch_op.drop_column("digest_id")

    with op.batch_alter_table("group_digest_recipients", schema=None) as batch_op:
        batch_op.drop_index("ix_group_digest_recipients_digest")

    op.drop_table("group_digest_recipients")

    with op.batch_alter_table("group_digests", schema=None) as batch_op:
        batch_op.drop_index("ix_group_digests_open")
        batch_op.drop_index(batch_op.f("ix_group_digests_library_id"))

    op.drop_table("group_digests")
