from urllib.parse import parse_qs, urlencode, urlsplit

from src.config import Settings


def validate_unsubscribe_base_url(settings: Settings) -> None:
    parts = urlsplit(settings.unsubscribe_base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.hostname is None
        or parts.fragment
        or "\r" in settings.unsubscribe_base_url
        or "\n" in settings.unsubscribe_base_url
        or any(
            character.isspace() or character in '<>"'
            for character in settings.unsubscribe_base_url
        )
        or (settings.app_env == "production" and parts.scheme != "https")
    ):
        raise ValueError("Unsubscribe base URL must be a safe absolute HTTP(S) URL.")
    try:
        parts.port
    except ValueError as error:
        raise ValueError("Unsubscribe base URL has an invalid port.") from error
    if "token" in parse_qs(parts.query):
        raise ValueError("Unsubscribe base URL must not predefine a token query parameter.")


def promotional_unsubscribe_headers(token: str, settings: Settings) -> dict[str, str]:
    validate_unsubscribe_base_url(settings)
    separator = "&" if "?" in settings.unsubscribe_base_url else "?"
    unsubscribe_url = (
        f"{settings.unsubscribe_base_url}{separator}{urlencode({'token': token})}"
    )
    if len(unsubscribe_url) > 900:
        raise ValueError("Unsubscribe URL is too long for an email header.")
    return {
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
