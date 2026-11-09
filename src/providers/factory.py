from src.config import Settings
from src.providers.base import EmailProvider
from src.providers.brevo import BrevoEmailProvider
from src.providers.resend import ResendEmailProvider
from src.providers.smtp import MailpitSMTPProvider


def build_email_provider(settings: Settings) -> EmailProvider:
    if settings.email_provider == "resend":
        return ResendEmailProvider(settings)
    if settings.email_provider == "brevo":
        return BrevoEmailProvider(settings)
    return MailpitSMTPProvider(settings)


def rate_limit_provider_name(settings: Settings) -> str:
    return "mailpit-smtp" if settings.email_provider == "mailpit" else settings.email_provider
