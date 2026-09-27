"""Shared Postgres/PostGIS connection helper for the GIS API."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from app import settings


def connect():
    """Open a new psycopg connection returning rows as dicts."""
    return psycopg.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        dbname=settings.POSTGRES_DB,
        row_factory=dict_row,
    )


def ping() -> bool:
    """Cheap liveness probe so /health cannot report ok while the DB is unreachable."""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False
