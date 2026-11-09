"""Represent ambiguous provider outcomes without retrying them.

Revision ID: 0005_delivery_unknown
Revises: 0004_provider_webhooks
Create Date: 2026-11-09
"""

from typing import Sequence

from alembic import op

revision: str = "0005_delivery_unknown"
down_revision: str | None = "0004_provider_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_constraint("ck_messages_status", type_="check")
        batch_op.create_check_constraint(
            "ck_messages_status",
            "status IN ('queued', 'processing', 'sent', 'retry', 'failed', 'cancelled', 'unknown')",
        )


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_constraint("ck_messages_status", type_="check")
        batch_op.create_check_constraint(
            "ck_messages_status",
            "status IN ('queued', 'processing', 'sent', 'retry', 'failed', 'cancelled')",
        )
