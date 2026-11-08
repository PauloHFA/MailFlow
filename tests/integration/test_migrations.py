from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_initial_migration_creates_and_downgrades_mailflow_schema(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "mailflow-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    table_names = set(inspect(engine).get_table_names())
    assert {
        "recipients",
        "templates",
        "messages",
        "events",
        "suppressions",
        "consents",
        "campaigns",
        "schedules",
        "dead_letters",
    } <= table_names
    engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(database_url)
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()
