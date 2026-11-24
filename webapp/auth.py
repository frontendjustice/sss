"""Accounts: password hashing, login sessions (an HttpOnly cookie) and who may do what.

Sign-up needs the class invite code. The account whose email matches ADMIN_EMAIL becomes the
admin (with no ADMIN_EMAIL set, the very first account does). Admins can reset a student's
password, since this pilot sends no email.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import LoginSession, User, setting

COOKIE = "qb_session"
SESSION_DAYS = 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), digest)
    except ValueError:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: Session, user: User, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=_token_hash(token), user_id=user.id, expires=time.time() + SESSION_DAYS * 86400))
    db.commit()
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=https)


def end_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        row = db.get(LoginSession, _token_hash(token))
        if row:
            db.delete(row)
            db.commit()
    response.delete_cookie(COOKIE)


def current_user(db: Session, request: Request) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.get(LoginSession, _token_hash(token))
    if not row or row.expires < time.time():
        return None
    user = db.get(User, row.user_id)
    if user and time.time() - user.last_seen > 300:
        user.last_seen = time.time()
        db.commit()
    return user


def require_user(db: Session, request: Request) -> User:
    user = current_user(db, request)
    if not user:
        raise HTTPException(401, "Please log in")
    return user


def require_admin(db: Session, request: Request) -> User:
    user = require_user(db, request)
    if not user.is_admin:
        raise HTTPException(403, "Only the admin can do that")
    return user


def sign_up(db: Session, email: str, name: str, password: str, invite: str) -> User:
    email = email.strip().lower()
    name = " ".join(name.split())[:100]
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    if len(name) < 2:
        raise HTTPException(400, "Enter your name")
    if len(password) < 8:
        raise HTTPException(400, "Use a password of at least 8 characters")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(400, "An account with this email already exists. Log in instead.")
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    first = (db.scalar(select(func.count()).select_from(User)) or 0) == 0
    becomes_admin = email == admin_email if admin_email else first
    code = setting(db, "invite_code") or os.environ.get("CLASS_INVITE_CODE", "")
    if not becomes_admin and (not code or not hmac.compare_digest(invite.strip().upper(), code.strip().upper())):
        raise HTTPException(400, "That class invite code is not right. Ask your class admin for it.")
    user = User(email=email, name=name, password_hash=hash_password(password),
                role="admin" if becomes_admin else "student", plan="pro" if becomes_admin else "free")
    db.add(user)
    db.commit()
    return user


def log_in(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not check_password(password, user.password_hash):
        raise HTTPException(400, "Email or password is not right")
    return user


def temporary_password() -> str:
    return "-".join(secrets.token_hex(2) for _ in range(3))   # e.g. 3fa2-91bc-07de
