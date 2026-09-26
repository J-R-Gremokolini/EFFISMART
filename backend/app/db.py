"""Connexion à la base (PostgreSQL + TimescaleDB ; SQLite en mémoire pour les tests)."""
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
        if url in ("sqlite://", "sqlite:///:memory:"):
            # Base en mémoire (tests) : une connexion unique partagée, sinon chaque connexion aurait sa base.
            return create_engine(url, connect_args=connect_args, poolclass=StaticPool)
        # Fichier SQLite (mode local 100 % Python).
        return create_engine(url, connect_args=connect_args)
    return create_engine(url, pool_pre_ping=True)


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db
