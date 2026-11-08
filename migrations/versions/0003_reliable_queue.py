"""Add persistent retry, campaign linkage, and dead-letter state.

Revision ID: 0003_reliable_queue
Revises: 0002_campaigns
Create Date: 2026-11-08
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_reliable_queue"
down_revision: str | None = "0002_campaigns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_constraint("ck_messages_status", type_="check")
        batch_op.create_check_constraint(
            "ck_messages_status",
            "status IN ('queued', 'processing', 'sent', 'retry', 'failed', 'cancelled')",
        )
        batch_op.add_column(sa.Column("campaign_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_messages_campaign_id_campaigns",
            "campaigns",
            ["campaign_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_messages_retry_due", ["status", "next_attempt_at"], unique=False
        )
        batch_op.create_index(
            "ix_messages_campaign_status", ["campaign_id", "status"], unique=False
        )

    op.create_table(
        "dead_letters",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_dead_letters_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_dead_letters_message"),
    )


def downgrade() -> None:
    op.drop_table("dead_letters")
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_index("ix_messages_campaign_status")
        batch_op.drop_index("ix_messages_retry_due")
        batch_op.drop_constraint("fk_messages_campaign_id_campaigns", type_="foreignkey")
        batch_op.drop_column("processing_started_at")
        batch_op.drop_column("next_attempt_at")
        batch_op.drop_column("campaign_id")
        batch_op.drop_constraint("ck_messages_status", type_="check")
        batch_op.create_check_constraint(
            "ck_messages_status",
            "status IN ('queued', 'processing', 'sent', 'retry', 'failed')",
        )
