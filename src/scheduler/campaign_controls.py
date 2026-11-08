from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.db.models import (
    Campaign,
    CampaignStatus,
    CampaignSchedule,
    Message,
    MessageStatus,
)


class InvalidCampaignTransition(ValueError):
    pass


def pause_campaign(session: Session, campaign: Campaign) -> None:
    if campaign.status != CampaignStatus.ACTIVE.value:
        raise InvalidCampaignTransition("Only active campaigns can be paused.")
    campaign.status = CampaignStatus.PAUSED.value
    session.execute(
        update(Message)
        .where(
            Message.campaign_id == campaign.id,
            Message.status.in_(
                (MessageStatus.QUEUED.value, MessageStatus.RETRY.value)
            ),
        )
        .values(enqueued_at=None)
    )
    session.commit()


def resume_campaign(session: Session, campaign: Campaign) -> None:
    if campaign.status != CampaignStatus.PAUSED.value:
        raise InvalidCampaignTransition("Only paused campaigns can be resumed.")
    campaign.status = CampaignStatus.ACTIVE.value
    session.commit()


def cancel_campaign(session: Session, campaign: Campaign) -> None:
    if campaign.status not in (
        CampaignStatus.ACTIVE.value,
        CampaignStatus.PAUSED.value,
    ):
        raise InvalidCampaignTransition(
            "Only active or paused campaigns can be cancelled."
        )
    campaign.status = CampaignStatus.CANCELLED.value
    session.execute(
        update(CampaignSchedule)
        .where(
            CampaignSchedule.campaign_id == campaign.id,
            CampaignSchedule.processed_at.is_(None),
        )
        .values(processed_at=datetime.now(timezone.utc))
    )
    session.execute(
        update(Message)
        .where(
            Message.campaign_id == campaign.id,
            Message.status.in_(
                (MessageStatus.QUEUED.value, MessageStatus.RETRY.value)
            ),
        )
        .values(
            status=MessageStatus.CANCELLED.value,
            enqueued_at=None,
            next_attempt_at=None,
        )
    )
    session.commit()
