from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import Settings

Base = declarative_base()


def create_sqlite_engine(settings: Settings):
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(settings.database_url, future=True, connect_args={"check_same_thread": False})


def make_session_factory(settings: Settings):
    engine = create_sqlite_engine(settings)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True), engine


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
