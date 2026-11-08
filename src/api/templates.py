from enum import StrEnum

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.dependencies import get_db
from src.db.models import EmailTemplate, MessageType
from src.rendering.templates import (
    TemplateValidationError,
    render_template,
    validate_template,
)

router = APIRouter(prefix="/api/v1/templates", tags=["templates"])


class TemplateType(StrEnum):
    TRANSACTIONAL = MessageType.TRANSACTIONAL.value
    PROMOTIONAL = MessageType.PROMOTIONAL.value


class TemplateCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=998)
    html_body: str = Field(min_length=1)
    text_body: str = Field(min_length=1)
    required_variables: list[str] = Field(default_factory=list)
    message_type: TemplateType = TemplateType.TRANSACTIONAL

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Template name must not contain line breaks.")
        return value

    @field_validator("required_variables")
    @classmethod
    def validate_required_variables(cls, values: list[str]) -> list[str]:
        if any(not value or value.strip() != value for value in values):
            raise ValueError("Required variable names must be non-empty and trimmed.")
        if len(set(values)) != len(values):
            raise ValueError("Required variable names must be unique.")
        return values


class TemplatePreview(BaseModel):
    variables: dict[str, object] = Field(default_factory=dict)


def _template_response(template: EmailTemplate) -> dict[str, object]:
    return {
        "id": template.id,
        "name": template.name,
        "version": template.version,
        "subject": template.subject,
        "html_body": template.html_body,
        "text_body": template.text_body,
        "required_variables": template.required_variables,
        "message_type": template.message_type,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_template(
    payload: TemplateCreate,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        validate_template(
            subject=payload.subject,
            html_body=payload.html_body,
            text_body=payload.text_body,
            required_variables=payload.required_variables,
        )
    except TemplateValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    latest_version = session.scalar(
        select(func.max(EmailTemplate.version)).where(EmailTemplate.name == payload.name)
    )
    template = EmailTemplate(
        name=payload.name,
        version=(latest_version or 0) + 1,
        subject=payload.subject,
        html_body=payload.html_body,
        text_body=payload.text_body,
        required_variables=payload.required_variables,
        message_type=payload.message_type.value,
    )
    session.add(template)
    session.commit()
    session.refresh(template)
    return _template_response(template)


@router.get("/{template_id}")
def get_template(
    template_id: str,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    template = session.get(EmailTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    return _template_response(template)


@router.post("/{template_id}/preview")
def preview_template(
    template_id: str,
    payload: TemplatePreview,
    session: Session = Depends(get_db),
) -> dict[str, str]:
    template = session.get(EmailTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found.")

    try:
        rendered = render_template(
            subject=template.subject,
            html_body=template.html_body,
            text_body=template.text_body,
            required_variables=template.required_variables,
            variables=payload.variables,
        )
    except TemplateValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "subject": rendered.subject,
        "html_body": rendered.html_body,
        "text_body": rendered.text_body,
    }
