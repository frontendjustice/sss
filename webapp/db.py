"""Database models. Postgres (Supabase) in production via DATABASE_URL; a local SQLite file otherwise.

Uploaded files are stored in the database itself (Blob) so a single connection string is the
only setting needed; that suits a pilot of a few users and can move to object storage later.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from sqlalchemy import (JSON, Boolean, Float, ForeignKey, Index, Integer, LargeBinary, String, Text,
                        create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def now() -> float:
    return time.time()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="student")   # admin | student
    plan: Mapped[str] = mapped_column(String(20), default="free")      # free | pro
    created: Mapped[float] = mapped_column(Float, default=now)
    last_seen: Mapped[float] = mapped_column(Float, default=now)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_pro(self) -> bool:
        return self.plan == "pro" or self.is_admin


class LoginSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires: Mapped[float] = mapped_column(Float)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # None = class course
    created: Mapped[float] = mapped_column(Float, default=now)


class Blob(Base):
    __tablename__ = "blobs"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    size: Mapped[int] = mapped_column(Integer)


class Material(Base):
    """An uploaded file and what the engine extracted from it (cleaned sections), prepared once."""
    __tablename__ = "materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"), nullable=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    visibility: Mapped[str] = mapped_column(String(20), default="private")   # class | private
    name: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    blob_sha: Mapped[str] = mapped_column(ForeignKey("blobs.sha256"))
    status: Mapped[str] = mapped_column(String(20), default="processing")   # processing | ready | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    sections: Mapped[list] = mapped_column(JSON, default=list)
    created: Mapped[float] = mapped_column(Float, default=now)


class Quiz(Base):
    __tablename__ = "quizzes"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="private")   # class | private
    title: Mapped[str] = mapped_column(String(255))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    requested: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[float] = mapped_column(Float, default=now)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)          # q, o, a, e, type, source, flag, ai
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    checked: Mapped[bool] = mapped_column(Boolean, default=False)   # a person reviewed it
    removed: Mapped[bool] = mapped_column(Boolean, default=False)


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    quiz_id: Mapped[int | None] = mapped_column(ForeignKey("quizzes.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(20))           # practice | exam
    question_ids: Mapped[list] = mapped_column(JSON)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created: Mapped[float] = mapped_column(Float, default=now)
    finished: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    answered: Mapped[int] = mapped_column(Integer, default=0)
    correct: Mapped[int] = mapped_column(Integer, default=0)


class Answer(Base):
    __tablename__ = "answers"
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"))
    chosen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correct: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[float] = mapped_column(Float, default=now)
    __table_args__ = (Index("ix_answers_user_question", "user_id", "question_id"),)


class Mark(Base):
    """A question a student marked to check later."""
    __tablename__ = "marks"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True)
    created: Mapped[float] = mapped_column(Float, default=now)


class Usage(Base):
    __tablename__ = "usage"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    generations: Mapped[int] = mapped_column(Integer, default=0)
    uploads: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class CacheEntry(Base):
    """Saved model results (page transcriptions, generated question batches): reused across users."""
    __tablename__ = "cache"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        data = Path(os.environ.get("DATA_DIR", "data")).resolve()
        data.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{data / 'app.db'}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str | None = None):
    url = url or database_url()
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _fk_on(conn, _):  # SQLite enforces foreign keys only when asked
            conn.execute("PRAGMA foreign_keys=ON")
    else:
        engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        lock_down(engine)
    return engine


def lock_down(engine) -> None:
    """Turn on row-level security for every app table, with no policies. Supabase's public Data API
    then sees nothing in them, while the app (connecting as the tables' owner) is unaffected."""
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY')


class DbCache:
    """The engine's cache interface (get/set) backed by the cache table."""

    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def get(self, key: str) -> dict | None:
        with self.sessions() as db:
            row = db.get(CacheEntry, key)
            return row.value if row else None

    def set(self, key: str, value: dict) -> None:
        with self.sessions() as db:
            row = db.get(CacheEntry, key)
            if row:
                row.value = value
            else:
                db.add(CacheEntry(key=key, value=value))
            db.commit()


def setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
