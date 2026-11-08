from datetime import datetime, timezone

from src.config import Settings
from src.queue.rate_limit import RateLimitExceeded, RedisRateLimiter
from src.queue.retries import compute_retry_delay
from src.queue.warmup import (
    get_reputation_snapshot,
    record_reputation_signal,
    reserve_warmup_capacity,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    def eval(self, _script: str, key_count: int, *values: object) -> list[int]:
        keys = [str(value) for value in values[:key_count]]
        limits = [int(value) for value in values[key_count::2]]
        for key, limit in zip(keys, limits, strict=True):
            if self.values.get(key, 0) >= limit:
                return [0, 7, keys.index(key) + 1]
        for key in keys:
            self.values[key] = self.values.get(key, 0) + 1
        return [1, 0]

    def hincrby(self, key: str, field: str, amount: int) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    def expire(self, _key: str, _seconds: int) -> bool:
        return True

    def hgetall(self, key: str) -> dict[str, int]:
        return self.hashes.get(key, {})


def test_retry_delay_grows_exponentially_with_a_one_hour_cap() -> None:
    assert compute_retry_delay(1, base_seconds=2) == 2
    assert compute_retry_delay(2, base_seconds=2) == 4
    assert compute_retry_delay(5, base_seconds=2) == 32
    assert compute_retry_delay(20, base_seconds=2) == 3600


def test_rate_limiter_enforces_provider_and_destination_domain_caps() -> None:
    instant = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    scenarios = (
        (
            Settings(
                _env_file=None,
                provider_rate_per_second=1,
                provider_daily_limit=10,
                recipient_domain_rate_per_second=10,
                recipient_domain_daily_limit=10,
            ),
            ("mailpit-smtp", "example.com", instant),
            ("mailpit-smtp", "other.com", instant),
            ":second:",
        ),
        (
            Settings(
                _env_file=None,
                provider_rate_per_second=10,
                provider_daily_limit=1,
                recipient_domain_rate_per_second=10,
                recipient_domain_daily_limit=10,
            ),
            ("mailpit-smtp", "example.com", instant),
            ("mailpit-smtp", "example.com", instant.replace(second=1)),
            ":day:",
        ),
        (
            Settings(
                _env_file=None,
                provider_rate_per_second=10,
                provider_daily_limit=10,
                recipient_domain_rate_per_second=1,
                recipient_domain_daily_limit=10,
            ),
            ("provider-a", "example.com", instant),
            ("provider-b", "example.com", instant),
            "domain:example.com:second:",
        ),
        (
            Settings(
                _env_file=None,
                provider_rate_per_second=10,
                provider_daily_limit=10,
                recipient_domain_rate_per_second=10,
                recipient_domain_daily_limit=1,
            ),
            ("provider-a", "example.com", instant),
            ("provider-b", "example.com", instant.replace(second=1)),
            "domain:example.com:day:",
        ),
    )
    for settings, first, second, expected_scope in scenarios:
        limiter = RedisRateLimiter(FakeRedis())
        limiter.reserve(first[0], first[1], settings, now=first[2])
        try:
            limiter.reserve(second[0], second[1], settings, now=second[2])
        except RateLimitExceeded as error:
            assert error.retry_after_seconds == 7
            assert expected_scope in error.scope
        else:
            raise AssertionError(f"Expected rate limit for {expected_scope}.")


def test_warmup_capacity_is_configurable_and_enforced_per_domain() -> None:
    redis = FakeRedis()
    settings = Settings(
        _env_file=None,
        warmup_enabled=True,
        warmup_daily_limit=5,
        warmup_domain_daily_limit=1,
    )
    instant = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    reserve_warmup_capacity(redis, "example.com", settings, now=instant)
    try:
        reserve_warmup_capacity(redis, "example.com", settings, now=instant)
    except RateLimitExceeded as error:
        assert error.retry_after_seconds == 7
    else:
        raise AssertionError("The domain warm-up cap should reject excess volume.")


def test_warmup_global_daily_limit_is_enforced_across_domains() -> None:
    redis = FakeRedis()
    settings = Settings(
        _env_file=None,
        warmup_enabled=True,
        warmup_daily_limit=1,
        warmup_domain_daily_limit=5,
    )
    instant = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    reserve_warmup_capacity(redis, "first.example", settings, now=instant)
    try:
        reserve_warmup_capacity(redis, "second.example", settings, now=instant)
    except RateLimitExceeded as error:
        assert "warmup:global" in error.scope
    else:
        raise AssertionError("The global warm-up cap should reject excess volume.")


def test_reputation_counters_track_daily_bounces_and_complaints() -> None:
    redis = FakeRedis()
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    record_reputation_signal(redis, "sent", now=now)
    record_reputation_signal(redis, "sent", now=now)
    record_reputation_signal(redis, "bounce", now=now)
    record_reputation_signal(redis, "complaint", now=now)

    snapshot = get_reputation_snapshot(redis, now=now)
    assert (snapshot.sent, snapshot.bounces, snapshot.complaints) == (2, 1, 1)
    assert snapshot.bounce_rate == 0.5
    assert snapshot.complaint_rate == 0.5
