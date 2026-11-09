import json
import logging
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for field in (
            "method",
            "route",
            "status_code",
            "duration_ms",
            "message_id",
            "error_type",
            "attempt",
            "reason",
            "limit_scope",
            "provider",
        ):
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        return json.dumps(entry, separators=(",", ":"))


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(JsonFormatter())
