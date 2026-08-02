"""Authentication router: signup, login, refresh, logout, and the JWT dependency.

Endpoints
    POST /auth/signup   -> create a user (409 if the email already exists)
    POST /auth/login    -> verify credentials, return access + refresh tokens
    POST /auth/refresh  -> exchange a refresh token for a new pair (rotating)
    POST /auth/logout   -> revoke a refresh token
    GET  /auth/me       -> echo the caller's identity (useful smoke test)

Other routers protect themselves by declaring `Depends(get_current_user)`.

Token model: access tokens are short-lived and stateless, so no database read is
needed to authorise a request. Refresh tokens live for weeks, so those are
tracked server-side by `jti` and can be revoked. Refreshing rotates the token --
the presented one is revoked as the new one is issued -- which bounds the value
of a stolen refresh token.

Still missing, and documented rather than implied: there is no role or
permission model. Every authenticated user has identical rights.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.database.models import RefreshToken, User
from backend.database.session import get_db
from backend.models.schemas import (
    LoginRequest, RefreshRequest, SignupRequest, TokenResponse, UserResponse)
from backend.utils.config import get_settings
from backend.utils.logging_config import logger
from backend.utils.rate_limit import get_rate_limiter
from backend.utils.security import (
    TokenError, create_access_token, create_refresh_token, decode_access_token,
    decode_refresh_token, hash_password, verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])

# auto_error=False so a missing Authorization header produces our own 401
# instead of FastAPI's default 403, keeping every auth failure a single status.
_bearer_scheme = HTTPBearer(auto_error=False)

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)

_settings = get_settings()
login_limiter = get_rate_limiter(
    max_attempts=_settings.login_max_attempts,
    window_seconds=_settings.login_window_seconds,
    prefix="login",
)


def normalize_email(email: str) -> str:
    """Lower-case and trim an email so lookups are case-insensitive."""
    return email.strip().lower()


def rate_limit_key(email: str, client_host: str | None) -> str:
    """Throttle per (account, source address).

    Keying on the pair means one attacker cannot lock every account by
    guessing, and a single account cannot be locked out from every location.
    """
    return f"{normalize_email(email)}|{client_host or 'unknown'}"


def get_user_by_email(db: Session, email: str) -> User | None:
    """Fetch a user by normalized email, or None."""
    return db.query(User).filter(User.email == normalize_email(email)).one_or_none()


def create_user(db: Session, email: str, password: str) -> User:
    """Persist a new user with a bcrypt-hashed password."""
    user = User(email=normalize_email(email), hashed_password=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Return the user if the password matches, else None.

    Hashes against a dummy value when the email is unknown so that "no such
    user" and "wrong password" take similar time and cannot be distinguished by
    response latency.
    """
    user = get_user_by_email(db, email)
    if user is None:
        # Verify against a syntactically valid hash to burn the same CPU.
        verify_password(password, "$2b$12$" + "." * 53)
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def issue_token_pair(db: Session, user: User) -> TokenResponse:
    """Mint an access + refresh pair and record the refresh token's jti."""
    settings = get_settings()
    access = create_access_token(subject=user.email)
    refresh, jti, expires_at = create_refresh_token(subject=user.email)

    # Stored naive-UTC to match the DateTime columns, which are not tz-aware.
    db.add(RefreshToken(jti=jti, user_id=user.id,
                        expires_at=expires_at.replace(tzinfo=None)))
    db.commit()

    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/signup", response_model=UserResponse,
             status_code=status.HTTP_201_CREATED)
def signup(req: SignupRequest, db: Session = Depends(get_db)) -> User:
    """Register a new account. Does not log the user in; call /auth/login next."""
    if get_user_by_email(db, req.email) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "An account with that email already exists.")
    user = create_user(db, req.email, req.password)
    logger.info("Created user id=%s", user.id)
    return user


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request,
          db: Session = Depends(get_db)) -> TokenResponse:
    """Exchange email + password for an access + refresh token pair."""
    key = rate_limit_key(req.email, request.client.host if request.client else None)

    if login_limiter.is_limited(key):
        retry_after = login_limiter.retry_after(key)
        logger.warning("Login rate limit hit.")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    user = authenticate_user(db, req.email, req.password)
    if user is None:
        login_limiter.record_failure(key)
        logger.info("Failed login attempt.")
        raise _CREDENTIALS_ERROR

    login_limiter.reset(key)
    return issue_token_pair(db, user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Exchange a valid refresh token for a new pair, revoking the old one.

    Rotation means a stolen refresh token is useful only until the legitimate
    client next refreshes, at which point the stolen copy is already revoked.
    """
    try:
        claims = decode_refresh_token(req.refresh_token)
    except TokenError as exc:
        logger.info("Rejected refresh token: %s", exc)
        raise _CREDENTIALS_ERROR from exc

    record = db.query(RefreshToken).filter(
        RefreshToken.jti == claims["jti"]).one_or_none()
    if record is None or not record.is_active():
        logger.info("Refresh token is unknown, revoked, or expired.")
        raise _CREDENTIALS_ERROR

    user = get_user_by_email(db, claims["sub"])
    if user is None:
        raise _CREDENTIALS_ERROR

    record.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return issue_token_pair(db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT,
             response_class=Response)
def logout(req: RefreshRequest, db: Session = Depends(get_db)) -> Response:
    """Revoke a refresh token.

    Returns 204 whether or not the token was valid, so this cannot be used to
    probe which tokens exist. Any already-issued *access* token keeps working
    until it expires -- the documented cost of stateless access tokens.
    """
    no_content = Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        claims = decode_refresh_token(req.refresh_token)
    except TokenError:
        return no_content

    record = db.query(RefreshToken).filter(
        RefreshToken.jti == claims["jti"]).one_or_none()
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
    return no_content


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Dependency that resolves a bearer token to a User, or raises 401.

    Re-reads the user on every request so a deleted account stops working
    immediately, even while its token is still within its lifetime. Refresh
    tokens presented here are rejected: they are not access credentials.
    """
    if credentials is None or not credentials.credentials:
        raise _CREDENTIALS_ERROR
    try:
        claims = decode_access_token(credentials.credentials)
    except TokenError as exc:
        logger.info("Rejected token: %s", exc)
        raise _CREDENTIALS_ERROR from exc

    user = get_user_by_email(db, claims["sub"])
    if user is None:
        raise _CREDENTIALS_ERROR
    return user


@router.get("/me", response_model=UserResponse)
def read_current_user(current_user: User = Depends(get_current_user)) -> User:
    """Return the authenticated caller. Handy for verifying a token by hand."""
    return current_user
