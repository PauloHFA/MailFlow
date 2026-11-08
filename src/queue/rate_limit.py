from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Protocol

from redis import Redis

from src.config import Settings

_RESERVE_SCRIPT = """
for i, key in ipairs(KEYS) do
    local current = tonumber(redis.call('GET', key) or '0')
    local limit = tonumber(ARGV[(i - 1) * 2 + 1])
    if current >= limit then
        local ttl = redis.call('TTL', key)
        if ttl < 1 then ttl = 1 end
        return {0, ttl, i}
    end
end
for i, key in ipairs(KEYS) do
    local count = redis.call('INCR', key)
    if count == 1 then
        redis.call('EXPIRE', key, tonumber(ARGV[(i - 1) * 2 + 2]))
    end
end
return {1, 0}
"""


class RedisCounter(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> list[int]: ...


@dataclass(frozen=True)
class RateLimit:
    key: str
    limit: int
    ttl_seconds: int


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int, scope: str) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        self.scope = scope
        super().__init__(f"Rate limit exceeded for {scope}.")


def reserve_limits(redis: RedisCounter, limits: tuple[RateLimit, ...]) -> None:
    if not limits:
        return
    result = redis.eval(
        _RESERVE_SCRIPT,
        len(limits),
        *(limit.key for limit in limits),
        *(value for limit in limits for value in (limit.limit, limit.ttl_seconds)),
    )
    allowed, retry_after = int(result[0]), int(result[1])
    if not allowed:
        limit_index = int(result[2]) - 1
        raise RateLimitExceeded(retry_after, limits[limit_index].key)


def _daily_window(now: datetime) -> tuple[str, int]:
    utc_now = now.astimezone(timezone.utc)
    tomorrow = datetime.combine(
        utc_now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return utc_now.strftime("%Y%m%d"), max(1, int((tomorrow - utc_now).total_seconds()))


class RedisRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def reserve(
        self,
        provider: str,
        recipient_domain: str,
        settings: Settings,
        *,
        now: datetime,
    ) -> None:
        utc_now = now.astimezone(timezone.utc)
        day, day_ttl = _daily_window(utc_now)
        second = int(utc_now.timestamp())
        provider = provider.casefold()
        domain = recipient_domain.casefold()
        reserve_limits(
            self._redis,
            (
                RateLimit(
                    f"mailflow:rate:provider:{provider}:second:{second}",
                    settings.provider_rate_per_second,
                    1,
                ),
                RateLimit(
                    f"mailflow:rate:provider:{provider}:day:{day}",
                    settings.provider_daily_limit,
                    day_ttl,
                ),
                RateLimit(
                    f"mailflow:rate:domain:{domain}:second:{second}",
                    settings.recipient_domain_rate_per_second,
                    1,
                ),
                RateLimit(
                    f"mailflow:rate:domain:{domain}:day:{day}",
                    settings.recipient_domain_daily_limit,
                    day_ttl,
                ),
            ),
        )


def redis_client(redis_url: str) -> Redis:
    return Redis.from_url(redis_url, decode_responses=True)
