"""Durable file storage in the database (Render erases the container disk on every restart).

    key = blobstore.put(b"...", "uploads/7/report.pdf", content_type="application/pdf", owner_id=user.id)
    data, ctype = blobstore.get(key)
"""
from __future__ import annotations

import mimetypes
import re

from sqlalchemy import delete, select

from .db import SessionLocal
from .models import StoredFile

_SAFE = re.compile(r"[^A-Za-z0-9._/-]+")


def safe_key(key: str) -> str:
    key = _SAFE.sub("-", key).strip("/")
    parts = [p for p in key.split("/") if p not in {"", ".", ".."}]
    if not parts:
        raise ValueError("empty file key")
    return "/".join(parts)[:1000]


def put(data: bytes, key: str, content_type: str | None = None, owner_id: int | None = None) -> str:
    key = safe_key(key)
    ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
    with SessionLocal() as s:
        row = s.get(StoredFile, key)
        if row is None:
            s.add(StoredFile(key=key, content=data, content_type=ctype, size=len(data), owner_id=owner_id))
        else:
            row.content, row.content_type, row.size = data, ctype, len(data)
        s.commit()
    return key


def get(key: str) -> tuple[bytes, str] | None:
    with SessionLocal() as s:
        row = s.get(StoredFile, safe_key(key))
        return (row.content, row.content_type) if row else None


def list_prefix(prefix: str) -> list[tuple[str, int]]:
    prefix = safe_key(prefix) + "/"
    with SessionLocal() as s:
        return [(k, n) for k, n in s.execute(select(StoredFile.key, StoredFile.size).where(StoredFile.key.startswith(prefix)))]


def remove(key: str) -> None:
    with SessionLocal() as s:
        s.execute(delete(StoredFile).where(StoredFile.key == safe_key(key)))
        s.commit()
