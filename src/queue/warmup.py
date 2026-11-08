import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from redis import Redis
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from src.config import Settings
from src.db.models import Campaign, CampaignStatus, MessageType
from src.queue.rate_limit import RateLimit, reserve_limits

logger = logging.getLogger("mailflow.warmup")


@dataclass(frozen=True)
class ReputationSnapshot:
    sent: int
    bounces: int
    complaints: int

    @property
    def bounce_rate(self) -> float:
        return self.bounces / self.sent if self.sent else 0.0

    @property
    def complaint_rate(self) -> float:
        return self.complaints / self.sent if self.sent else 0.0


def reserve_warmup_capacity(
    redis: Redis,
    recipient_domain: str,
    settings: Settings,
    *,
    now: datetime,
) -> None:
    if not settings.warmup_enabled:
        return
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    utc_now = now.astimezone(timezone.utc)
    day = utc_now.strftime("%Y%m%d")
    next_day = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)

    ttl = max(1, int(((next_day + timedelta(days=1)) - utc_now).total_seconds()))
    reserve_limits(
        redis,
        (
            RateLimit(
                f"mailflow:warmup:global:{day}",
                settings.warmup_daily_limit,
                ttl,
            ),
            RateLimit(
                f"mailflow:warmup:domain:{recipient_domain.casefold()}:{day}",
                settings.warmup_domain_daily_limit,
                ttl,
            ),
        ),
    )


def record_reputation_signal(
    redis: Redis,
    signal: str,
    *,
    now: datetime,
) -> None:
    if signal not in {"sent", "bounce", "complaint"}:
        raise ValueError("signal must be sent, bounce, or complaint.")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    utc_now = now.astimezone(timezone.utc)
    key = f"mailflow:reputation:{utc_now:%Y%m%d}"
    field = {"sent": "sent", "bounce": "bounces", "complaint": "complaints"}[signal]
    redis.hincrby(key, field, 1)
    redis.expire(key, 90 * 24 * 60 * 60)


def get_reputation_snapshot(redis: Redis, *, now: datetime) -> ReputationSnapshot:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    key = f"mailflow:reputation:{now.astimezone(timezone.utc):%Y%m%d}"
    values = redis.hgetall(key)
    return ReputationSnapshot(
        sent=int(values.get("sent", 0)),
        bounces=int(values.get("bounces", 0)),
        complaints=int(values.get("complaints", 0)),
    )


def pause_promotional_campaigns_if_needed(
    session: Session,
    snapshot: ReputationSnapshot,
    settings: Settings,
) -> int:
    if snapshot.sent == 0:
        return 0
    breached = (
        snapshot.bounce_rate >= settings.reputation_bounce_threshold
        or snapshot.complaint_rate >= settings.reputation_complaint_threshold
    )
    if not breached:
        return 0
    result = session.execute(
        update(Campaign)
        .where(
            Campaign.message_type == MessageType.PROMOTIONAL.value,
            Campaign.status == CampaignStatus.ACTIVE.value,
        )
        .values(status=CampaignStatus.PAUSED.value)
    )
    session.commit()
    paused = result.rowcount or 0
    logger.critical(
        "promotional_campaigns_paused_for_reputation",
        extra={
            "paused_campaigns": paused,
            "bounce_rate": snapshot.bounce_rate,
            "complaint_rate": snapshot.complaint_rate,
        },
    )
    return paused


def handle_reputation_signal(
    session_factory: sessionmaker[Session],
    redis: Redis,
    settings: Settings,
    signal: str,
    *,
    now: datetime,
) -> int:
    record_reputation_signal(redis, signal, now=now)
    snapshot = get_reputation_snapshot(redis, now=now)
    with session_factory() as session:
        return pause_promotional_campaigns_if_needed(session, snapshot, settings)
