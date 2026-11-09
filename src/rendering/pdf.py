from datetime import datetime, timezone
from io import BytesIO

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

MAX_PDF_BYTES = 5 * 1024 * 1024


def validate_pdf_attachment(content: bytes) -> None:
    if (
        len(content) < 16
        or len(content) > MAX_PDF_BYTES
        or not content.startswith(b"%PDF-")
        or not content.rstrip().endswith(b"%%EOF")
    ):
        raise ValueError("PDF attachment is invalid or exceeds the 5 MB limit.")


def build_delivery_report_pdf(
    *,
    start_at: datetime,
    end_at: datetime,
    messages: dict[str, int],
    events: dict[str, int],
) -> bytes:
    for value in (start_at, end_at):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Report period must include timezone-aware datetimes.")
    start_utc = start_at.astimezone(timezone.utc)
    end_utc = end_at.astimezone(timezone.utc)
    if end_utc <= start_utc:
        raise ValueError("Report end must be later than report start.")

    output = BytesIO()
    document = canvas.Canvas(output, pagesize=letter, pageCompression=1)
    _, height = letter
    document.setTitle("MailFlow Daily Delivery Report")
    document.setAuthor("MailFlow")
    document.setFont("Helvetica-Bold", 18)
    document.drawString(54, height - 60, "MailFlow Delivery Report")
    document.setFont("Helvetica", 10)
    document.drawString(
        54,
        height - 82,
        f"Period (UTC): {start_utc:%Y-%m-%d %H:%M} to {end_utc:%Y-%m-%d %H:%M}",
    )

    y = height - 125
    document.setFont("Helvetica-Bold", 12)
    document.drawString(54, y, "Messages by current status")
    y -= 22
    document.setFont("Helvetica", 10)
    for status, count in sorted(messages.items()):
        document.drawString(66, y, f"{status}: {count}")
        y -= 16
    y -= 12
    document.setFont("Helvetica-Bold", 12)
    document.drawString(54, y, "Delivery events in period")
    y -= 22
    document.setFont("Helvetica", 10)
    for event_type, count in sorted(events.items()):
        document.drawString(66, y, f"{event_type}: {count}")
        y -= 16

    document.showPage()
    document.save()
    content = output.getvalue()
    validate_pdf_attachment(content)
    return content
