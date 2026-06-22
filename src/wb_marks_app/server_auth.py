from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wb_marks_app.models import AppConfig
from wb_marks_app.server_models import AppSettingsModel, PasswordResetTokenModel, UserModel, WorkflowRunModel


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LOGIN_RE = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")
_PBKDF2_ITERATIONS = 600_000


@dataclass(slots=True)
class RegisteredUser:
    id: str
    email: str
    login: str


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_login(value: str) -> str:
    return value.strip().lower()


def validate_registration(email: str, login: str, password: str, password_confirm: str) -> None:
    if not _EMAIL_RE.match(email):
        raise ValueError("Invalid email.")
    if not _LOGIN_RE.match(login):
        raise ValueError("Login must be 3-64 chars: letters, digits, dot, underscore, hyphen.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if password != password_confirm:
        raise ValueError("Passwords do not match.")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iteration_text, salt, digest_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iteration_text)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return hmac.compare_digest(digest.hex(), digest_hex)


def register_user(session: Session, email: str, login: str, password: str, password_confirm: str) -> UserModel:
    normalized_email = normalize_email(email)
    normalized_login = normalize_login(login)
    validate_registration(normalized_email, normalized_login, password, password_confirm)

    existing = session.execute(
        select(UserModel).where(func.lower(UserModel.login) == normalized_login)
    ).scalars().first()
    if existing is not None:
        raise ValueError("Login already exists.")

    user = UserModel(
        email=normalized_email,
        login=normalized_login,
        password_hash=hash_password(password),
    )
    session.add(user)
    session.flush()
    _claim_legacy_state(session, user)
    return user


def authenticate_user(session: Session, login: str, password: str) -> UserModel | None:
    normalized_login = normalize_login(login)
    user = session.execute(
        select(UserModel).where(func.lower(UserModel.login) == normalized_login)
    ).scalars().first()
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def get_user_by_id(session: Session, user_id: str) -> UserModel | None:
    return session.get(UserModel, user_id)


def request_password_reset(
    session: Session,
    config: AppConfig,
    email: str,
    login: str,
) -> tuple[UserModel | None, str | None]:
    normalized_email = normalize_email(email)
    normalized_login = normalize_login(login)
    user = session.execute(
        select(UserModel)
        .where(func.lower(UserModel.login) == normalized_login)
        .where(func.lower(UserModel.email) == normalized_email)
    ).scalars().first()
    if user is None:
        return None, None

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    reset_token = PasswordResetTokenModel(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=config.password_reset_ttl_minutes),
    )
    session.add(reset_token)
    session.flush()
    return user, raw_token


def validate_reset_password(password: str, password_confirm: str) -> None:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if password != password_confirm:
        raise ValueError("Passwords do not match.")


def get_reset_user(session: Session, raw_token: str) -> UserModel | None:
    token = _get_active_reset_token(session, raw_token)
    if token is None:
        return None
    return session.get(UserModel, token.user_id)


def consume_password_reset(session: Session, raw_token: str, password: str, password_confirm: str) -> UserModel:
    validate_reset_password(password, password_confirm)
    token = _get_active_reset_token(session, raw_token)
    if token is None:
        raise ValueError("Reset link is invalid or expired.")
    user = session.get(UserModel, token.user_id)
    if user is None:
        raise ValueError("User not found.")
    user.password_hash = hash_password(password)
    token.used_at = datetime.now(timezone.utc)
    session.add(user)
    session.add(token)
    session.flush()
    return user


def _claim_legacy_state(session: Session, user: UserModel) -> None:
    has_claimed_settings = session.execute(
        select(AppSettingsModel.id).where(AppSettingsModel.user_id.is_not(None)).limit(1)
    ).first()
    if has_claimed_settings:
        return

    legacy_settings = session.execute(
        select(AppSettingsModel)
        .where(AppSettingsModel.user_id.is_(None))
        .order_by(AppSettingsModel.id.asc())
        .limit(1)
    ).scalars().first()
    if legacy_settings is not None:
        legacy_settings.user_id = user.id

    legacy_runs = session.execute(
        select(WorkflowRunModel).where(WorkflowRunModel.user_id.is_(None))
    ).scalars().all()
    for run in legacy_runs:
        run.user_id = user.id


def _get_active_reset_token(session: Session, raw_token: str) -> PasswordResetTokenModel | None:
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    token = session.execute(
        select(PasswordResetTokenModel).where(PasswordResetTokenModel.token_hash == token_hash)
    ).scalars().first()
    if token is None:
        return None
    expires_at = token.expires_at
    now = (
        datetime.now(timezone.utc)
        if getattr(expires_at, "tzinfo", None) is not None
        else datetime.now(timezone.utc).replace(tzinfo=None)
    )
    if token.used_at is not None or expires_at < now:
        return None
    return token
