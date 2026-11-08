from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.dependencies import get_db
from src.db.models import (
    Campaign,
    CampaignSchedule,
    CampaignStatus,
    EmailTemplate,
    MessageType,
    Recipient,
    RecipientStatus,
)
from src.scheduler.windows import (
    compute_relative_reminder,
    compute_send_window,
    normalize_utc,
)

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])


class ScheduleRequest(BaseModel):
    kind: Literal["once", "relative"]
    send_at: datetime | None = None
    due_at: datetime | None = None
    days_before: int | None = Field(default=None, ge=1)
    timezone: str = "UTC"

    @model_validator(mode="after")
    def validate_rule(self) -> "ScheduleRequest":
        if self.kind == "once":
            if self.send_at is None or self.due_at is not None or self.days_before is not None:
                raise ValueError("A one-time schedule requires send_at only.")
            if self.send_at.tzinfo is None or self.send_at.utcoffset() is None:
                raise ValueError("send_at must include a timezone.")
        elif (
            self.due_at is None
            or self.days_before is None
            or self.send_at is not None
        ):
            raise ValueError("A relative schedule requires due_at and days_before only.")
        elif self.due_at.tzinfo is None or self.due_at.utcoffset() is None:
            raise ValueError("due_at must include a timezone.")

        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be a valid IANA timezone.") from error
        return self


class CampaignCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    template_id: str = Field(min_length=36, max_length=36)
    message_type: Literal["transactional", "promotional"] = "transactional"
    purpose: str | None = Field(default=None, min_length=1, max_length=120)
    recipient_ids: list[str] = Field(min_length=1, max_length=10000)
    variables: dict[str, object] = Field(default_factory=dict)
    schedule: ScheduleRequest

    @model_validator(mode="after")
    def validate_recipients(self) -> "CampaignCreate":
        if len(set(self.recipient_ids)) != len(self.recipient_ids):
            raise ValueError("recipient_ids must not contain duplicates.")
        if self.message_type == MessageType.PROMOTIONAL.value and not self.purpose:
            raise ValueError("Promotional campaigns require a consent purpose.")
        return self


@router.post("", status_code=status.HTTP_201_CREATED)
def create_campaign(
    payload: CampaignCreate,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    template = session.get(EmailTemplate, payload.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    if template.message_type != payload.message_type:
        raise HTTPException(
            status_code=422,
            detail="Campaign message type must match its template.",
        )
    if payload.message_type == MessageType.PROMOTIONAL.value:
        raise HTTPException(
            status_code=422,
            detail=(
                "Promotional campaigns are disabled until one-click unsubscribe "
                "and provider suppression webhooks are implemented."
            ),
        )

    recipients = session.scalars(
        select(Recipient).where(Recipient.id.in_(payload.recipient_ids))
    ).all()
    recipients_by_id = {recipient.id: recipient for recipient in recipients}
    if len(recipients_by_id) != len(payload.recipient_ids):
        raise HTTPException(status_code=422, detail="One or more recipients were not found.")

    now = datetime.now(timezone.utc)
    for recipient in recipients:
        if recipient.status != RecipientStatus.ACTIVE.value:
            raise HTTPException(status_code=422, detail="Campaign includes a suppressed recipient.")

    campaign = Campaign(
        name=payload.name,
        template_id=template.id,
        message_type=payload.message_type,
        purpose=payload.purpose,
        status=CampaignStatus.ACTIVE.value,
        variables=payload.variables,
    )
    session.add(campaign)
    session.flush()

    schedule_rule: dict[str, object] = {"kind": payload.schedule.kind}
    if payload.schedule.kind == "once":
        schedule_rule["send_at"] = payload.schedule.send_at.isoformat()
    else:
        schedule_rule["due_at"] = payload.schedule.due_at.isoformat()
        schedule_rule["days_before"] = payload.schedule.days_before

    created_schedules: list[CampaignSchedule] = []
    for recipient in recipients:
        recipient_timezone = recipient.timezone or payload.schedule.timezone
        try:
            if payload.schedule.kind == "once":
                next_execution = compute_send_window(
                    payload.schedule.send_at,
                    recipient_timezone,
                )
            else:
                next_execution = compute_relative_reminder(
                    payload.schedule.due_at,
                    payload.schedule.days_before,
                    recipient_timezone,
                )
        except ValueError as error:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(error)) from error

        schedule = CampaignSchedule(
            campaign_id=campaign.id,
            recipient_id=recipient.id,
            rule=schedule_rule,
            next_execution=next_execution.astimezone(timezone.utc),
            timezone=recipient_timezone,
        )
        session.add(schedule)
        created_schedules.append(schedule)

    session.commit()
    for schedule in created_schedules:
        session.refresh(schedule)
    return {
        "id": campaign.id,
        "status": campaign.status,
        "schedules": [
            {
                "id": schedule.id,
                "recipient_id": schedule.recipient_id,
                "next_execution": normalize_utc(schedule.next_execution).astimezone(
                    ZoneInfo(schedule.timezone)
                ),
                "timezone": schedule.timezone,
            }
            for schedule in created_schedules
        ],
    }
