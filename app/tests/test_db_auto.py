"""PLAINBOARD_DATABASE=auto (the compose file): a new installation starts on the bundled PostgreSQL, one with
its data in SQLite stays on it until the move is asked for, and a failed move goes on with SQLite."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from plainboard import config, db_move, migrate

UNREACHABLE = "postgresql://plainboard:plainboard@127.0.0.1:1/plainboard"


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    monkeypatch.setenv("PLAINBOARD_DATA_DIR", str(folder))
    monkeypatch.setattr(db_move, "BUNDLED", UNREACHABLE)
    config.get_config.cache_clear()
    yield folder
    config.get_config.cache_clear()


def _use(monkeypatch, database: str) -> config.Config:
    monkeypatch.setenv("PLAINBOARD_DATABASE", database)
    config.get_config.cache_clear()
    return config.get_config()


def _sqlite_installation(folder, monkeypatch):
    _use(monkeypatch, "")
    migrate.upgrade()
    return folder / "plainboard.db"


def _accounts(sqlite_file) -> int:
    engine = create_engine(f"sqlite:///{sqlite_file}")
    try:
        with engine.connect() as conn:
            return conn.scalar(text("SELECT count(*) FROM accounts"))
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("existing", "marker", "database", "postgres"),
    [
        (False, None, "auto", True),  # new: PostgreSQL
        (True, None, "auto", False),  # in SQLite, not asked: stays
        (True, "requested", "auto", True),  # asked for in Settings
        (True, "moved", "auto", True),  # moved before
        (False, None, "", False),  # a compose file from before
        (True, "requested", "", False),
        (False, None, "sqlite", False),
        (True, "moved", "sqlite", False),  # back to SQLite
        (False, None, UNREACHABLE, True),
        (True, None, UNREACHABLE, True),
    ],
)
def test_resolution(data, monkeypatch, existing, marker, database, postgres):
    sqlite_file = data / "plainboard.db"
    if existing:
        _sqlite_installation(data, monkeypatch)
    if marker:
        (db_move.requested if marker == "requested" else db_move.marker)(sqlite_file).write_text("x")
    cfg = _use(monkeypatch, database)
    assert cfg.postgres is postgres
    assert cfg.database_url.startswith("postgresql+psycopg://" if postgres else "sqlite+aiosqlite://")


def test_empty_or_unreadable_sqlite_file(data, monkeypatch):
    data.mkdir(parents=True)
    sqlite_file = data / "plainboard.db"
    sqlite_file.touch()  # no tables: not an installation
    assert _use(monkeypatch, "auto").postgres
    sqlite_file.write_bytes(b"not a database" * 100)  # what cannot be read is kept, never replaced by an empty PostgreSQL
    assert not _use(monkeypatch, "auto").postgres


def test_unknown_value_is_refused(data, monkeypatch):
    monkeypatch.setenv("PLAINBOARD_DATABASE", "mysql://x")
    config.get_config.cache_clear()
    with pytest.raises(ValueError):
        config.get_config()


def test_existing_sqlite_is_never_moved_without_being_asked(data, monkeypatch):
    sqlite_file = _sqlite_installation(data, monkeypatch)
    before = _accounts(sqlite_file)
    monkeypatch.setattr(db_move, "move", lambda *a, **k: pytest.fail("moved without being asked"))
    _use(monkeypatch, "auto")
    migrate.upgrade()
    migrate.upgrade()
    assert not config.get_config().postgres
    assert _accounts(sqlite_file) == before
    assert not db_move.marker(sqlite_file).exists()


def test_new_installation_without_postgresql_does_not_start_on_sqlite(data, monkeypatch):
    _use(monkeypatch, "auto")
    with pytest.raises(OperationalError):
        migrate.upgrade()
    assert not (data / "plainboard.db").exists()  # the next start is still a new installation


def test_asked_move_that_cannot_run_goes_on_with_sqlite(data, monkeypatch):
    sqlite_file = _sqlite_installation(data, monkeypatch)
    digest = hashlib.sha256(sqlite_file.read_bytes()).hexdigest()
    db_move.requested(sqlite_file).write_text("x")
    cfg = _use(monkeypatch, "auto")
    assert cfg.postgres
    migrate.upgrade()
    assert not cfg.postgres and cfg.database_url.startswith("sqlite")
    assert "not reachable" in db_move.failed(sqlite_file).read_text()
    assert not db_move.requested(sqlite_file).exists() and not db_move.marker(sqlite_file).exists()
    assert hashlib.sha256(sqlite_file.read_bytes()).hexdigest() == digest
    assert not _use(monkeypatch, "auto").postgres  # and stays on SQLite at the next start
