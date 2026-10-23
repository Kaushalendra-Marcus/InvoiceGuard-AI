"""Database engine and session management (SQLite)."""

from pathlib import Path
from threading import Lock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_engine_lock = Lock()
_engines: dict[str, "object"] = {}
_session_makers: dict[str, sessionmaker] = {}


class Base(DeclarativeBase):
    pass


def _configure_sqlite(dbapi_connection, _record) -> None:
    """Enable WAL and a generous busy timeout so parallel requests queue
    instead of failing with 'database is locked'."""
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


def get_engine(db_path: Path | None = None):
    """Create (or return the cached) engine for the given database path.

    A per-path cache keeps tests isolated while production uses one engine.
    """
    path = Path(db_path or settings.db_path)
    key = str(path)

    with _engine_lock:
        if key not in _engines:
            path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(
                f"sqlite:///{path}",
                connect_args={"timeout": 30, "check_same_thread": False},
                pool_pre_ping=True,
            )
            event.listen(engine, "connect", _configure_sqlite)
            _engines[key] = engine
            _session_makers[key] = sessionmaker(bind=engine, expire_on_commit=False)
        return _engines[key]


def session_factory(db_path: Path | None = None) -> sessionmaker:
    get_engine(db_path)
    return _session_makers[str(Path(db_path or settings.db_path))]


def init_db(db_path: Path | None = None) -> None:
    from . import models  # noqa: F401  (register models on the Base)

    engine = get_engine(db_path)
    models.Base.metadata.create_all(engine)


def dispose_engines() -> None:
    """Close all cached engines (used mainly by tests)."""
    with _engine_lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
        _session_makers.clear()


__all__ = ["get_engine", "init_db", "session_factory", "dispose_engines", "Base"]
