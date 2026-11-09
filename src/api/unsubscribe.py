from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from src.api.dependencies import get_db, get_settings
from src.config import Settings
from src.db.models import Consent
from src.suppression.unsubscribe import (
    UnsubscribeTokenError,
    UnsubscribeTokenExpired,
    verify_unsubscribe_token,
)

router = APIRouter(prefix="/api/v1/unsubscribe", tags=["unsubscribe"])
MAX_ONE_CLICK_BYTES = 128


async def _read_one_click_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ONE_CLICK_BYTES:
            raise HTTPException(status_code=413, detail="One-click form body is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _consent_id(token: str, settings: Settings) -> str:
    if len(settings.unsubscribe_signing_secret.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=503,
            detail="Unsubscribe token verification is not configured.",
        )
    try:
        return verify_unsubscribe_token(token, settings.unsubscribe_signing_secret)
    except UnsubscribeTokenExpired as error:
        raise HTTPException(status_code=410, detail="Unsubscribe token has expired.") from error
    except UnsubscribeTokenError as error:
        raise HTTPException(status_code=400, detail="Unsubscribe token is invalid.") from error


@router.get("", response_class=HTMLResponse)
def unsubscribe_confirmation(
    token: str = Query(min_length=1, max_length=512),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    consent_id = _consent_id(token, settings)
    if session.get(Consent, consent_id) is None:
        raise HTTPException(status_code=404, detail="Consent record not found.")
    return HTMLResponse(
        "<!doctype html><html><body>"
        "<p>Confirm that you want to stop promotional email for this subscription.</p>"
        f'<form method="post" action="/api/v1/unsubscribe?token={token}">'
        '<button type="submit">Unsubscribe</button></form></body></html>'
    )


@router.post("")
async def unsubscribe_one_click(
    request: Request,
    token: str = Query(min_length=1, max_length=512),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    if request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().casefold() != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(status_code=415, detail="One-click form content type is required.")
    body = await _read_one_click_body(request)
    try:
        parsed = parse_qs(body.decode("ascii"), strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="One-click form body is invalid.") from error
    if parsed != {"List-Unsubscribe": ["One-Click"]}:
        raise HTTPException(status_code=400, detail="One-click form body is invalid.")

    consent_id = _consent_id(token, settings)
    consent = session.get(Consent, consent_id)
    if consent is None:
        raise HTTPException(status_code=404, detail="Consent record not found.")
    if consent.revoked_at is None:
        consent.revoked_at = datetime.now(timezone.utc)
        session.commit()
    return JSONResponse({"status": "unsubscribed"})
