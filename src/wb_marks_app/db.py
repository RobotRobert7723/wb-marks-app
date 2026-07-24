from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from wb_marks_app.config import load_config


class Base(DeclarativeBase):
    metadata = MetaData()


_engine = None
_session_factory: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        config = load_config()
        connect_args = {"check_same_thread": False} if config.database_url.startswith("sqlite") else {}
        _engine = create_engine(config.database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
        _session_factory = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope():
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    from wb_marks_app import server_models

    engine = get_engine()
    config = load_config()
    if not config.database_url.startswith("sqlite"):
        with engine.begin() as conn:
            if config.database_schema:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{config.database_schema}"'))
            if server_models.TEKSHER_SCHEMA:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{server_models.TEKSHER_SCHEMA}"'))
    Base.metadata.create_all(bind=engine)
    _run_runtime_migrations(engine, config.database_schema)
    from wb_marks_app.services.gpc_catalog import seed_gpc_catalog

    with session_scope() as session:
        seed_gpc_catalog(session)


def _run_runtime_migrations(engine, schema: str) -> None:
    from wb_marks_app import server_models

    inspector = inspect(engine)
    app_settings_columns = {
        column["name"] for column in inspector.get_columns("app_settings", schema=schema or None)
    }
    user_columns = {
        column["name"] for column in inspector.get_columns("users", schema=schema or None)
    }
    workflow_run_columns = {
        column["name"] for column in inspector.get_columns("workflow_runs", schema=schema or None)
    }
    workflow_run_item_columns = {
        column["name"] for column in inspector.get_columns("workflow_run_items", schema=schema or None)
    }
    label_print_job_columns = {
        column["name"] for column in inspector.get_columns("label_print_jobs", schema=schema or None)
    }
    mapping_schema = server_models.TEKSHER_SCHEMA or None
    mapping_columns = {
        column["name"] for column in inspector.get_columns("mapping", schema=mapping_schema)
    }
    table_name_settings = _qualified_table_name("app_settings", schema)
    table_name_users = _qualified_table_name("users", schema)
    table_name_runs = _qualified_table_name("workflow_runs", schema)
    table_name_run_items = _qualified_table_name("workflow_run_items", schema)
    table_name_label_print_jobs = _qualified_table_name("label_print_jobs", schema)
    table_name_mapping = _qualified_table_name("mapping", mapping_schema or "")
    with engine.begin() as conn:
        if "wb_store_id" not in user_columns:
            conn.execute(text(f'ALTER TABLE {table_name_users} ADD COLUMN wb_store_id VARCHAR(64) DEFAULT \'\''))
        if "user_id" not in app_settings_columns:
            conn.execute(text(f'ALTER TABLE {table_name_settings} ADD COLUMN user_id VARCHAR(36)'))
        if "wb_store_id" not in app_settings_columns:
            conn.execute(text(f'ALTER TABLE {table_name_settings} ADD COLUMN wb_store_id VARCHAR(64) DEFAULT \'\''))
        if "supplier_name" not in app_settings_columns:
            conn.execute(text(f'ALTER TABLE {table_name_settings} ADD COLUMN supplier_name VARCHAR(255) DEFAULT \'\''))
        if "production_address" not in app_settings_columns:
            conn.execute(text(f'ALTER TABLE {table_name_settings} ADD COLUMN production_address TEXT DEFAULT \'\''))
        if "teksher_uot_name" not in app_settings_columns:
            conn.execute(text(f'ALTER TABLE {table_name_settings} ADD COLUMN teksher_uot_name VARCHAR(255) DEFAULT \'\''))
        if "teksher_gcp" not in app_settings_columns:
            conn.execute(text(f'ALTER TABLE {table_name_settings} ADD COLUMN teksher_gcp VARCHAR(32) DEFAULT \'\''))
        if "user_id" not in workflow_run_columns:
            conn.execute(text(f'ALTER TABLE {table_name_runs} ADD COLUMN user_id VARCHAR(36)'))
        if "label_template" not in workflow_run_item_columns:
            conn.execute(text(f'ALTER TABLE {table_name_run_items} ADD COLUMN label_template VARCHAR(32) DEFAULT \'srad\''))
        if "label_payload_json" not in workflow_run_item_columns:
            conn.execute(text(f'ALTER TABLE {table_name_run_items} ADD COLUMN label_payload_json TEXT DEFAULT \'{{}}\''))
        if "run_item_id" not in label_print_job_columns:
            conn.execute(text(f'ALTER TABLE {table_name_label_print_jobs} ADD COLUMN run_item_id VARCHAR(36)'))
        if "full_name" not in mapping_columns:
            conn.execute(text(f'ALTER TABLE {table_name_mapping} ADD COLUMN full_name TEXT DEFAULT \'\''))
        conn.execute(
            text(
                f"UPDATE {table_name_mapping} "
                "SET full_name = 'Костюм спортивный' "
                "WHERE COALESCE(full_name, '') = '' "
                "AND UPPER(product_type) IN ('КОСТЮМ СПОРТИВНЫЙ', 'КОСТЮМЫ СПОРТИВНЫЕ')"
            )
        )


def _qualified_table_name(name: str, schema: str) -> str:
    if not schema:
        return f'"{name}"'
    return f'"{schema}"."{name}"'
