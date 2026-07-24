from __future__ import annotations

import smtplib
from email.message import EmailMessage

from wb_marks_app.models import AppConfig


def smtp_configured(config: AppConfig) -> bool:
    return bool(config.smtp_host and config.smtp_from_email)


def send_password_reset_email(config: AppConfig, to_email: str, login: str, reset_link: str) -> None:
    if not smtp_configured(config):
        raise RuntimeError("SMTP is not configured.")

    message = EmailMessage()
    message["Subject"] = "Восстановление пароля WB Marks App"
    message["From"] = config.smtp_from_email
    message["To"] = to_email
    message.set_content(
        "\n".join(
            [
                f"Логин: {login}",
                "",
                "Чтобы сбросить пароль, откройте ссылку:",
                reset_link,
                "",
                f"Ссылка действует {config.password_reset_ttl_minutes} минут.",
            ]
        )
    )

    if config.smtp_use_ssl:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30) as server:
            _smtp_login(server, config)
            server.send_message(message)
        return

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
        if config.smtp_use_tls:
            server.starttls()
        _smtp_login(server, config)
        server.send_message(message)


def _smtp_login(server: smtplib.SMTP, config: AppConfig) -> None:
    if config.smtp_username:
        server.login(config.smtp_username, config.smtp_password)
