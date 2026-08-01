"""Authentication router: signup, login, and the JWT-bearer dependency.

Endpoints
    POST /auth/signup  -> create a user (409 if the email already exists)
    POST /auth/login   -> verify credentials, return a signed JWT
    GET  /auth/me      -> echo the caller's identity (useful smoke test)

Other routers protect themselves by declaring `Depends(get_current_user)`.

Scope note: this covers authentication only. There is no role/permission model,
no refresh-token rotation, and no server-side revocation -- a leaked token is
valid until it expires. Those are the next things to add before this guards
anything sensitive.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.database.models import User
from backend.database.session import get_db
from backend.models.schemas import (
    LoginRequest, SignupRequest, TokenResponse, UserResponse)
from backend.utils.config import get_settings
from backend.utils.logging_config import logger
from backend.utils.security import (
    TokenError, create_access_token, decode_access_token, hash_password,
    verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])

# auto_error=False so a missing Authorization header produces our own 401
# instead of FastAPI's default 403, keeping every auth failure a single status.
_bearer_scheme = HTTPBearer(auto_error=False)

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


def normalize_email(email: str) -> str:
    """Lower-case and trim an email so lookups are case-insensitive."""
    return email.strip().lower()


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
def login(req: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Exchange email + password for a bearer token."""
    user = authenticate_user(db, req.email, req.password)
    if user is None:
        logger.info("Failed login attempt.")
        raise _CREDENTIALS_ERROR
    expire_minutes = get_settings().access_token_expire_minutes
    return TokenResponse(
        access_token=create_access_token(subject=user.email),
        expires_in=expire_minutes * 60,
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Dependency that resolves a bearer token to a User, or raises 401.

    Re-reads the user on every request so a deleted account stops working
    immediately, even while its token is still within its lifetime.
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
