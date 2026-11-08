from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class RecipientStatus(StrEnum):
    ACTIVE = "active"
    SUPPRESSED = "suppressed"


class MessageType(StrEnum):
    TRANSACTIONAL = "transactional"
    PROMOTIONAL = "promotional"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CampaignStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


def new_id() -> str:
    return str(uuid4())


class Recipient(Base):
    __tablename__ = "recipients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default=RecipientStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("email", name="uq_recipients_email"),
        CheckConstraint(
            "status IN ('active', 'suppressed')",
            name="ck_recipients_status",
        ),
    )


class EmailTemplate(Base):
    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False)
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    required_variables: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    message_type: Mapped[str] = mapped_column(
        String(20), default=MessageType.TRANSACTIONAL.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_templates_name_version"),
        CheckConstraint("version > 0", name="ck_templates_version_positive"),
        CheckConstraint(
            "message_type IN ('transactional', 'promotional')",
            name="ck_templates_message_type",
        ),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recipient_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recipients.id", ondelete="RESTRICT"), nullable=False
    )
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("templates.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=MessageStatus.QUEUED.value, nullable=False
    )
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    variables: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'sent', 'retry', 'failed', 'cancelled')",
            name="ck_messages_status",
        ),
        CheckConstraint(
            "message_type IN ('transactional', 'promotional')",
            name="ck_messages_message_type",
        ),
        CheckConstraint("attempts >= 0", name="ck_messages_attempts_nonnegative"),
        Index("ix_messages_recipient_created", "recipient_id", "created_at"),
        Index("ix_messages_retry_due", "status", "next_attempt_at"),
        Index("ix_messages_campaign_status", "campaign_id", "status"),
    )


class MessageEvent(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_events_message_occurred", "message_id", "occurred_at"),
    )


class Suppression(Base):
    __tablename__ = "suppressions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("email", name="uq_suppressions_email"),)


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recipient_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(120), nullable=False)
    legal_basis: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_consents_recipient_purpose", "recipient_id", "purpose", "revoked_at"),
    )


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("templates.id", ondelete="RESTRICT"), nullable=False
    )
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        String(20), default=CampaignStatus.ACTIVE.value, nullable=False
    )
    variables: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "message_type IN ('transactional', 'promotional')",
            name="ck_campaigns_message_type",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'cancelled', 'completed')",
            name="ck_campaigns_status",
        ),
    )


class CampaignSchedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False
    )
    rule: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    next_execution: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("campaign_id", "recipient_id", name="uq_schedule_campaign_recipient"),
        Index("ix_schedules_due", "processed_at", "next_execution"),
    )


class DeadLetter(Base):
    __tablename__ = "dead_letters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("message_id", name="uq_dead_letters_message"),
        CheckConstraint("attempts >= 0", name="ck_dead_letters_attempts_nonnegative"),
    )
