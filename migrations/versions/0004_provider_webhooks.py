"""Persist provider event identifiers for webhook idempotency.

Revision ID: 0004_provider_webhooks
Revises: 0003_reliable_queue
Create Date: 2026-11-08
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004_provider_webhooks"
down_revision: str | None = "0003_reliable_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("provider", sa.String(length=40), nullable=True))
        batch_op.add_column(
            sa.Column("provider_event_id", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )
        batch_op.create_unique_constraint(
            "uq_events_provider_event_id",
            ["provider", "provider_event_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_constraint("uq_events_provider_event_id", type_="unique")
        batch_op.drop_column("received_at")
        batch_op.drop_column("provider_event_id")
        batch_op.drop_column("provider")
