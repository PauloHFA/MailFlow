from dataclasses import dataclass
from html.parser import HTMLParser

from jinja2 import Environment, StrictUndefined, TemplateError, meta, select_autoescape
from jinja2.sandbox import SandboxedEnvironment


class TemplateValidationError(ValueError):
    """Raised when an email template cannot be safely rendered."""


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html_body: str
    text_body: str


class _ImageAltChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images_without_alt = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "img" and "alt" not in {name.casefold() for name, _ in attrs}:
            self.images_without_alt += 1


def _environment(*, autoescape: bool) -> Environment:
    return SandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=autoescape,
        enable_async=False,
    )


def validate_template(
    *,
    subject: str,
    html_body: str,
    text_body: str,
    required_variables: list[str],
) -> None:
    if not subject.strip() or not html_body.strip() or not text_body.strip():
        raise TemplateValidationError("Subject, HTML body, and text body are required.")
    if "\r" in subject or "\n" in subject:
        raise TemplateValidationError("Subject template must not contain a line break.")
    image_checker = _ImageAltChecker()
    image_checker.feed(html_body)
    if image_checker.images_without_alt:
        raise TemplateValidationError("Every image in the HTML template must declare alt text.")

    declared = set(required_variables)
    if len(declared) != len(required_variables):
        raise TemplateValidationError("Required variable names must be unique.")

    for template_source in (subject, html_body, text_body):
        try:
            parsed = _environment(autoescape=False).parse(template_source)
        except TemplateError as error:
            raise TemplateValidationError(f"Invalid template syntax: {error}") from error
        undeclared = meta.find_undeclared_variables(parsed) - declared
        if undeclared:
            variables = ", ".join(sorted(undeclared))
            raise TemplateValidationError(
                f"Template variables are not declared as required: {variables}."
            )


def render_template(
    *,
    subject: str,
    html_body: str,
    text_body: str,
    required_variables: list[str],
    variables: dict[str, object],
) -> RenderedEmail:
    validate_template(
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        required_variables=required_variables,
    )

    missing = sorted(set(required_variables) - variables.keys())
    if missing:
        raise TemplateValidationError(
            f"Missing required template variables: {', '.join(missing)}."
        )

    try:
        rendered_subject = _environment(autoescape=False).from_string(subject).render(variables)
        rendered_html = _environment(autoescape=True).from_string(html_body).render(variables)
        rendered_text = _environment(autoescape=False).from_string(text_body).render(variables)
    except TemplateError as error:
        raise TemplateValidationError(f"Unable to render email template: {error}") from error

    if "\r" in rendered_subject or "\n" in rendered_subject:
        raise TemplateValidationError("Rendered subject must not contain a line break.")
    if not rendered_subject.strip():
        raise TemplateValidationError("Rendered subject must not be empty.")

    return RenderedEmail(
        subject=rendered_subject,
        html_body=rendered_html,
        text_body=rendered_text,
    )
