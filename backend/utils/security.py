"""Password hashing and JWT helpers.

Everything here is a small pure function over its arguments (plus process-level
config), so it can be unit-tested without a database, a web server, or an LLM.

Password hashing uses passlib's bcrypt backend. Token signing uses PyJWT with a
symmetric HS256 secret; that is appropriate for a single-service deployment.
Splitting the API across services later would mean moving to asymmetric signing
(RS256) so verifiers do not need the signing key.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from backend.utils.config import get_settings
from backend.utils.logging_config import logger

# bcrypt truncates silently past 72 bytes in some implementations and raises in
# others; we reject longer inputs at the API boundary instead of guessing.
MAX_PASSWORD_BYTES = 72

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Filled lazily by _get_secret() when no JWT_SECRET_KEY is configured.
_EPHEMERAL_SECRET: str | None = None


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or badly signed."""


# RFC 7518 §3.2: an HMAC key for HS256 should be at least as long as the hash
# output. A short secret is brute-forceable offline from any captured token.
MIN_SECRET_BYTES = 32

_WARNED_SHORT_SECRET = False


def _get_secret() -> str:
    """Return the JWT signing secret.

    Falls back to a random per-process secret so the app still boots
    unconfigured. That fallback invalidates every token on restart and breaks
    horizontal scaling, so it warns loudly rather than failing silently.
    """
    global _EPHEMERAL_SECRET, _WARNED_SHORT_SECRET
    configured = get_settings().jwt_secret_key
    if configured:
        if len(configured.encode("utf-8")) < MIN_SECRET_BYTES and not _WARNED_SHORT_SECRET:
            _WARNED_SHORT_SECRET = True
            logger.warning(
                "JWT_SECRET_KEY is only %s bytes; RFC 7518 recommends at least "
                "%s for HS256. A short secret can be brute-forced offline from "
                "a single captured token. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"',
                len(configured.encode("utf-8")), MIN_SECRET_BYTES)
        return configured
    if _EPHEMERAL_SECRET is None:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
        logger.warning(
            "JWT_SECRET_KEY is not set. Generated an ephemeral signing key: all "
            "issued tokens become invalid when this process restarts, and other "
            "workers cannot verify them. Set JWT_SECRET_KEY before deploying."
        )
    return _EPHEMERAL_SECRET


def password_too_long(password: str) -> bool:
    """True if the password exceeds what the bcrypt backend can hash."""
    return len(password.encode("utf-8")) > MAX_PASSWORD_BYTES


def hash_password(password: str) -> str:
    """Hash a plaintext password. Raises ValueError if it is too long."""
    if password_too_long(password):
        raise ValueError(
            f"Password exceeds {MAX_PASSWORD_BYTES} bytes, which bcrypt cannot hash."
        )
    return _pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Constant-time-ish check of a plaintext password against a stored hash.

    Returns False (rather than raising) on malformed hashes so callers can treat
    every failure mode as "bad credentials".
    """
    if password_too_long(plain_password):
        return False
    try:
        return _pwd_context.verify(plain_password, hashed_password)
    except ValueError as exc:  # unknown hash format, truncated hash, ...
        logger.warning("Password verification failed on a malformed hash: %s", exc)
        return False


ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def _create_token(subject: str, token_type: str, expires_delta: timedelta,
                  now: datetime | None = None) -> tuple[str, dict[str, Any]]:
    """Sign a JWT and return it alongside its claims.

    Every token carries a `type` so a refresh token cannot be replayed as an
    access token, and a `jti` so individual tokens can be referenced (and, for
    refresh tokens, revoked) without storing the token itself.
    """
    settings = get_settings()
    issued_at = now or datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "jti": secrets.token_urlsafe(24),
        "iat": issued_at,
        "exp": issued_at + expires_delta,
    }
    token = jwt.encode(claims, _get_secret(), algorithm=settings.jwt_algorithm)
    return token, claims


def create_access_token(
    subject: str,
    expires_minutes: int | None = None,
    now: datetime | None = None,
) -> str:
    """Sign a short-lived access token whose `sub` claim is the user's email.

    `now` is injectable so tests can mint already-expired tokens without
    sleeping or patching the clock globally.
    """
    settings = get_settings()
    minutes = (settings.access_token_expire_minutes if expires_minutes is None
               else expires_minutes)
    token, _ = _create_token(subject, ACCESS_TOKEN_TYPE,
                             timedelta(minutes=minutes), now)
    return token


def create_refresh_token(
    subject: str,
    expires_days: int | None = None,
    now: datetime | None = None,
) -> tuple[str, str, datetime]:
    """Sign a long-lived refresh token.

    Returns (token, jti, expires_at) so the caller can record the jti for
    revocation. The token itself is never stored.
    """
    settings = get_settings()
    days = (settings.refresh_token_expire_days if expires_days is None
            else expires_days)
    token, claims = _create_token(subject, REFRESH_TOKEN_TYPE,
                                  timedelta(days=days), now)
    return token, claims["jti"], claims["exp"]


def decode_token(token: str, expected_type: str | None = None) -> dict[str, Any]:
    """Verify signature, expiry and (optionally) token type; return the claims.

    Raises TokenError on any failure; callers map that to HTTP 401.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(token, _get_secret(), algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid.") from exc
    if not claims.get("sub"):
        raise TokenError("Token is missing a subject claim.")
    if expected_type is not None and claims.get("type") != expected_type:
        raise TokenError(
            f"Expected a {expected_type} token, got {claims.get('type')!r}.")
    return claims


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify a token and require that it is an access token."""
    return decode_token(token, expected_type=ACCESS_TOKEN_TYPE)


def decode_refresh_token(token: str) -> dict[str, Any]:
    """Verify a token and require that it is a refresh token."""
    return decode_token(token, expected_type=REFRESH_TOKEN_TYPE)
