"""SQLAlchemy ORM models for application metadata (not the user's analytics DB)."""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import (Column, Integer, String, DateTime, ForeignKey)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class RefreshToken(Base):
    """Server-side record of an issued refresh token, so it can be revoked.

    Access tokens stay stateless -- checking a denylist on every request would
    cost a query per call, and their lifetime is short by design. Refresh tokens
    live for weeks, so those are tracked: we store the `jti` (never the token
    itself, which is a credential) and mark it revoked on logout or rotation.
    """

    __tablename__ = "refresh_tokens"
    id = Column(Integer, primary_key=True)
    jti = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def is_active(self, now: datetime | None = None) -> bool:
        """True if this token is neither revoked nor past its expiry."""
        moment = now or datetime.utcnow()
        return self.revoked_at is None and self.expires_at > moment


class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    path = Column(String(1024))
    created_at = Column(DateTime, default=datetime.utcnow)


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    filename = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
