from functools import lru_cache
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_settings


@lru_cache
def get_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def session_scope() -> Iterator[Session]:
    factory = get_session_factory(get_settings().database_url)
    with factory() as session:
        yield session
