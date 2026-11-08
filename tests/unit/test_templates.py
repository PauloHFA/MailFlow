import pytest

from src.rendering.templates import TemplateValidationError, render_template


def test_render_template_returns_html_and_plain_text() -> None:
    rendered = render_template(
        subject="Hello {{ name }}",
        html_body="<p>Hello {{ name }}</p>",
        text_body="Hello {{ name }}",
        required_variables=["name"],
        variables={"name": "Ada"},
    )

    assert rendered.subject == "Hello Ada"
    assert rendered.html_body == "<p>Hello Ada</p>"
    assert rendered.text_body == "Hello Ada"


def test_render_template_escapes_html_values() -> None:
    rendered = render_template(
        subject="Hello",
        html_body="<p>{{ name }}</p>",
        text_body="Hello {{ name }}",
        required_variables=["name"],
        variables={"name": "<script>alert(1)</script>"},
    )

    assert rendered.html_body == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"


def test_render_template_fails_clearly_when_required_variable_is_missing() -> None:
    with pytest.raises(TemplateValidationError, match="name"):
        render_template(
            subject="Hello {{ name }}",
            html_body="<p>{{ name }}</p>",
            text_body="Hello {{ name }}",
            required_variables=["name"],
            variables={},
        )


def test_render_template_rejects_header_injection_in_subject() -> None:
    with pytest.raises(TemplateValidationError, match="line break"):
        render_template(
            subject="{{ subject }}",
            html_body="<p>Message</p>",
            text_body="Message",
            required_variables=["subject"],
            variables={"subject": "Hello\r\nBcc: victim@example.com"},
        )


def test_template_validation_rejects_undeclared_variables() -> None:
    with pytest.raises(TemplateValidationError, match="not declared"):
        render_template(
            subject="Hello {{ name }}",
            html_body="<p>{{ name }}</p>",
            text_body="Hello {{ name }}",
            required_variables=[],
            variables={"name": "Ada"},
        )


def test_template_validation_requires_alt_text_on_images() -> None:
    with pytest.raises(TemplateValidationError, match="alt text"):
        render_template(
            subject="Hello",
            html_body='<img src="logo.png">',
            text_body="Hello",
            required_variables=[],
            variables={},
        )


def test_template_validation_accepts_decorative_image_with_empty_alt() -> None:
    rendered = render_template(
        subject="Hello",
        html_body='<img src="divider.png" alt="">',
        text_body="Hello",
        required_variables=[],
        variables={},
    )

    assert 'alt=""' in rendered.html_body
