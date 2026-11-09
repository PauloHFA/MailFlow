from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.dependencies import require_operations_access
from src.domain.checks import check_domain_records

router = APIRouter(
    prefix="/api/v1/domain",
    tags=["domain"],
    dependencies=[Depends(require_operations_access)],
)


@router.get("/check")
def get_domain_check(
    domain: Annotated[str, Query(min_length=3, max_length=253)],
    dkim_selectors: Annotated[list[str] | None, Query(max_length=10)] = None,
) -> dict[str, object]:
    try:
        return check_domain_records(domain, dkim_selectors or [])
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
