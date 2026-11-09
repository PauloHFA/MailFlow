from datetime import datetime, timezone

import pytest

from src.rendering.pdf import (
    MAX_PDF_BYTES,
    build_delivery_report_pdf,
    validate_pdf_attachment,
)


def test_delivery_report_generates_a_valid_pdf_under_the_attachment_limit() -> None:
    start = datetime(2026, 11, 7, tzinfo=timezone.utc)
    end = datetime(2026, 11, 8, tzinfo=timezone.utc)

    document = build_delivery_report_pdf(
        start_at=start,
        end_at=end,
        messages={"sent": 8, "failed": 1},
        events={"delivered": 7, "complaint": 1},
    )

    assert document.startswith(b"%PDF-")
    assert len(document) <= MAX_PDF_BYTES
    validate_pdf_attachment(document)


@pytest.mark.parametrize("content", [b"", b"not a pdf", b"%PDF-"])
def test_pdf_attachment_rejects_invalid_or_truncated_documents(content: bytes) -> None:
    with pytest.raises(ValueError):
        validate_pdf_attachment(content)


def test_pdf_attachment_rejects_documents_over_five_megabytes() -> None:
    with pytest.raises(ValueError, match="5 MB"):
        validate_pdf_attachment(b"%PDF-" + b"x" * MAX_PDF_BYTES)
