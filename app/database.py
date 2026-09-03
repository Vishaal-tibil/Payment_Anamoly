from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DB_PATH

# Ensure the database's own directory exists -- DB_PATH is configurable
# (see app/config.py) so in a container it points at a mounted volume,
# whose parent may not exist on first boot.
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_wal_mode(dbapi_connection, connection_record):
    """WAL instead of SQLite's default rollback-journal mode -- lets
    concurrent reads proceed without contending with each other (or a
    writer) the way the default mode does. Confirmed via direct
    measurement: 5 identical concurrent requests took ~2x one request's
    time instead of running near-flat. journal_mode is stored in the
    database file itself (not per-connection), so this is a one-time
    real change to data/payments.db, not per-request overhead -- set on
    every connect so it's re-applied if the file is ever recreated (e.g.
    a `git checkout` restoring an older copy, which has happened before
    in this project).
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
