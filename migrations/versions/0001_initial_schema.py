"""Create the initial MailFlow transactional schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-08
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recipients",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suppressed')", name="ck_recipients_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_recipients_email"),
    )
    op.create_table(
        "templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("required_variables", sa.JSON(), nullable=False),
        sa.Column("message_type", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "message_type IN ('transactional', 'promotional')",
            name="ck_templates_message_type",
        ),
        sa.CheckConstraint("version > 0", name="ck_templates_version_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_templates_name_version"),
    )
    op.create_index("ix_templates_name", "templates", ["name"], unique=False)
    op.create_table(
        "suppressions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_suppressions_email"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("recipient_id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("message_type", sa.String(length=20), nullable=False),
        sa.Column("variables", sa.JSON(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "message_type IN ('transactional', 'promotional')",
            name="ck_messages_message_type",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_messages_attempts_nonnegative"),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'sent', 'retry', 'failed')",
            name="ck_messages_status",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["recipients.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["template_id"], ["templates.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_messages_idempotency_key"),
    )
    op.create_index(
        "ix_messages_recipient_created",
        "messages",
        ["recipient_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_events_message_occurred",
        "events",
        ["message_id", "occurred_at"],
        unique=False,
    )

def downgrade() -> None:
    op.drop_index("ix_events_message_occurred", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_messages_recipient_created", table_name="messages")
    op.drop_table("messages")
    op.drop_table("suppressions")
    op.drop_index("ix_templates_name", table_name="templates")
    op.drop_table("templates")
    op.drop_table("recipients")
