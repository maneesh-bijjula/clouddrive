import hashlib
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, text

from .config import settings


@lru_cache
def engine():
    config = settings()
    return create_engine(
        config.database_url,
        pool_pre_ping=True,
        pool_size=config.db_pool_size,
        max_overflow=config.db_max_overflow,
        pool_timeout=config.db_pool_timeout_seconds,
    )


def migrate():
    with engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(1987052403)"))
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, checksum text NOT NULL)"
            )
        )
        for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            existing = conn.execute(
                text("SELECT checksum FROM schema_migrations WHERE name=:name"), {"name": path.name}
            ).scalar()
            if existing and existing != checksum:
                raise RuntimeError(f"Migration checksum changed: {path.name}")
            if not existing:
                conn.exec_driver_sql(sql)
                conn.execute(
                    text("INSERT INTO schema_migrations VALUES (:name,:checksum)"),
                    {"name": path.name, "checksum": checksum},
                )
