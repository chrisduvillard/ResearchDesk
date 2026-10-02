"""Local owner setup, Argon2id, opaque server-side sessions and CSRF."""

import hashlib
import json
import os
import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel, Field
from . import db

router = APIRouter(prefix="/api/v2/auth", tags=["Owner"])
HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, type=Type.ID)
COOKIE = "research_owner"
DUMMY = HASHER.hash(secrets.token_urlsafe(32))


def audit(conn, action, entity_type, entity_id, details=None, actor="owner"):
    conn.execute(
        "INSERT INTO audit_log(actor,action,entity_type,entity_id,created_at,details_json) VALUES(?,?,?,?,?,?)",
        (
            actor,
            action,
            entity_type,
            str(entity_id),
            db.iso(),
            json.dumps(details or {}, sort_keys=True),
        ),
    )


def setup_owner(conn, password):
    if not 15 <= len(password) <= 1024:
        raise ValueError("Use a password of 15–1024 characters")
    hashed = HASHER.hash(password)
    with conn:
        conn.execute(
            "INSERT INTO owner VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash,changed_at=excluded.changed_at",
            (hashed, db.iso()),
        )
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM login_attempts")
        audit(conn, "reset", "owner", 1, actor="local-setup")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def same_origin(request):
    origin = request.headers.get("origin")
    expected = os.environ.get("APP_ORIGIN") or str(request.base_url).rstrip("/")
    if origin and origin != expected:
        raise HTTPException(403, "Cross-origin editing is not allowed")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site editing is not allowed")


def session(request):
    token = request.cookies.get(COOKIE, "")
    if len(token) > 200:
        return None
    with db.database() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?",
            (digest(token), db.iso()),
        ).fetchone()
        return dict(row) if row else None


def require_owner(request: Request):
    owner = session(request)
    if not owner:
        raise HTTPException(401, "Owner login required")
    same_origin(request)
    if not secrets.compare_digest(
        owner["csrf_token"], request.headers.get("x-csrf-token", "")
    ):
        raise HTTPException(403, "CSRF token required")
    return owner


class Login(BaseModel):
    password: str = Field(max_length=1024)


@router.get("")
def current(request: Request):
    owner = session(request)
    with db.database() as conn:
        configured = bool(conn.execute("SELECT 1 FROM owner").fetchone())
    return dict(
        authenticated=bool(owner),
        configured=configured,
        csrf_token=owner["csrf_token"] if owner else None,
    )


@router.post("/login")
def login(payload: Login, request: Request, response: Response):
    same_origin(request)
    now = db.utcnow()
    with db.database() as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM login_attempts WHERE attempted_at<?",
                (db.iso(now - timedelta(minutes=15)),),
            )
            if conn.execute("SELECT count(*) FROM login_attempts").fetchone()[0] >= 5:
                raise HTTPException(
                    429,
                    "Too many attempts; retry in 15 minutes",
                    headers={"Retry-After": "900"},
                )
            # Count before the expensive hash, including concurrent requests.
            attempt = conn.execute(
                "INSERT INTO login_attempts(attempted_at) VALUES(?)", (db.iso(now),)
            ).lastrowid
            row = conn.execute("SELECT password_hash FROM owner WHERE id=1").fetchone()
        try:
            valid = HASHER.verify(row[0] if row else DUMMY, payload.password) and bool(
                row
            )
        except (VerificationError, InvalidHashError):
            valid = False
        if not valid:
            raise HTTPException(401, "Invalid credentials")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            # A concurrent password reset must not allow the old credential in.
            if not conn.execute(
                "SELECT 1 FROM owner WHERE password_hash=?", (row[0],)
            ).fetchone():
                raise HTTPException(401, "Credentials changed; login again")
            conn.execute("DELETE FROM login_attempts WHERE id=?", (attempt,))
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (db.iso(now),))
            conn.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (digest(token), csrf, db.iso(now), db.iso(now + timedelta(hours=12))),
            )
            audit(conn, "login", "owner", 1)
    response.set_cookie(
        COOKIE,
        token,
        max_age=43200,
        secure=os.environ.get("APP_SECURE_COOKIES", "1") != "0",
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True, "csrf_token": csrf}


@router.post("/logout")
def logout(response: Response, owner=Depends(require_owner)):
    with db.database() as conn, conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (owner["token_hash"],))
        audit(conn, "logout", "owner", 1)
    response.delete_cookie(COOKIE, path="/")
    return {"authenticated": False}
