"""C4 integrity tests — paper-trading ledger schema must stay ORM-shaped.

Single authoritative source of truth: the SQLAlchemy models
(``PaperPosition`` / ``PaperPendingOrder``). A past drift hazard was the
hand-maintained raw ``CREATE TABLE`` strings inside ``paper_trading.py``
mirroring the same tables. These tests bake that invariant in: the live
SQLite ledger must match the ORM column-for-column (name, declared type
affinity, NOT NULL) at all times.

Rules enforced here (audit C4):
  * the live DB is never dropped or recreated by tests — read-only probe;
  * ORM metadata wins: any column the ORM declares that the raw DDL omitted
    is a real drift and must be reported, not papered over;
  * additive ALTER migrations are allowed by the runtime, never destructive
    rebuilds.
"""
import importlib
import os
import sqlite3
import sqlite3
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_ROOT / "intradayai.db"

PAPER_TABLES = ("paper_positions", "paper_pending_orders")

# Column-difference aspect we want the ORM to be authoritative for.
_NON_DRIFT = {"ix_paper_positions_user", "ix_paper_pending_orders_user"}


def _live_columns(table: str) -> dict[str, tuple]:
    con = sqlite3.connect(DB_PATH)
    try:
        cols = con.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        con.close()
    return {r[1]: (r[2], r[3], r[5]) for r in cols}  # name -> (type, notnull, pk)


def _orm_columns(cls) -> dict[str, tuple]:
    """Project the ORM class onto ``(affinity, notnull)`` the same way the raw
    DDL checker would, so the comparison is apples-to-apples."""
    out = {}
    for c in cls.__table__.columns:
        affinity = c.type.affinity
        # Map SQLAlchemy affinity → the SQLite storage classes we expect.
        mapping = {
            "TEXT": "TEXT",
            "INTEGER": "INTEGER",
            "FLOAT": "REAL",
            "NUMERIC": "REAL",
        }
        out[c.name] = (mapping.get(affinity, affinity or "TEXT"),
                       not c.nullable and not c.primary_key)
    return out


def test_ledger_tables_exist_and_are_orm_shaped():
    assert DB_PATH.exists(), f"ledger DB missing at {DB_PATH}"

    orm = importlib.import_module("app.models.models")
    for table in PAPER_TABLES:
        live = _live_columns(table)
        assert live, f"table {table!r} missing from live ledger"

        # Index names prove ORM-created bootstrap (ix_* prefix).
        con = sqlite3.connect(DB_PATH)
        try:
            idxs = {r[1] for r in con.execute("PRAGMA index_list(%s)" % table).fetchall()}
        finally:
            con.close()
        assert any(i.startswith("ix_") for i in idxs), f"{table} has no ORM index"


def test_orm_columns_match_live_ledger_columns():
    """ORM metadata is the single source of truth — every ORM column must exist
    in the live table with a compatible affinity; nothing extra ORM-side."""

    from app.models.models import PaperPendingOrder, PaperPosition

    for table, cls in (("paper_positions", PaperPosition),
                       ("paper_pending_orders", PaperPendingOrder)):
        live = _live_columns(table)
        orm = _orm_columns(cls)
        missing = set(orm) - set(live)
        extra = set(live) - set(orm)
        assert not missing, f"ORM declares columns missing live: {table} {sorted(missing)}"
        # Extra live columns degrade gracefully (additive legacy), but report.
        if extra:
            pytest.fail(
                f"lead-drift: live {table} has columns no longer in ORM: {sorted(extra)}"
            )
        for name, (aff, notnull) in orm.items():
            live_aff, live_notnull, _pk = live[name]
            assert live_aff.upper() == aff.upper(), f"{table}.{name} type drift"
            # Not-null: if ORM requires it, the live column must too.
            if notnull:
                assert live_notnull == 1, f"{table}.{name} should be NOT NULL"


def test_paper_trading_ddl_is_not_a_dropper():
    """The raw DDL the service uses must stay additive (CREATE IF NOT EXISTS) —
    never a DROP/rebuild that would wipe paper positions/trades."""
    src = (BACKEND_ROOT / "app" / "services" / "paper_trading.py").read_text(
        encoding="utf-8"
    )
    lower = src.lower()
    assert "drop table" not in lower
    assert "drop index" not in lower
    assert "recreate" not in lower
    assert "create table if not exists" in lower
