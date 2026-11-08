"""Add consent and scheduled campaign persistence.

Revision ID: 0002_campaigns
Revises: 0001_initial
Create Date: 2026-11-08
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_campaigns"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("recipient_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=120), nullable=False),
        sa.Column("legal_basis", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["recipients.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_consents_recipient_purpose",
        "consents",
        ["recipient_id", "purpose", "revoked_at"],
        unique=False,
    )
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("message_type", sa.String(length=20), nullable=False),
        sa.Column("purpose", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("variables", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "message_type IN ('transactional', 'promotional')",
            name="ck_campaigns_message_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'cancelled', 'completed')",
            name="ck_campaigns_status",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["templates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_id", sa.String(length=36), nullable=False),
        sa.Column("rule", sa.JSON(), nullable=False),
        sa.Column("next_execution", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["recipients.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "recipient_id",
            name="uq_schedule_campaign_recipient",
        ),
    )
    op.create_index(
        "ix_schedules_due",
        "schedules",
        ["processed_at", "next_execution"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_schedules_due", table_name="schedules")
    op.drop_table("schedules")
    op.drop_table("campaigns")
    op.drop_index("ix_consents_recipient_purpose", table_name="consents")
    op.drop_table("consents")
