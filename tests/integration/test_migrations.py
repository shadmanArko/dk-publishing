from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest

from dk_publishing.adapters.persistence.migrate import MigrationError, apply_migrations
from dk_publishing.domain.planning import Action
from dk_publishing.domain.status import VariantStatus
from tests.integration.conftest import MIGRATIONS

EXPECTED_TABLES = {
    "social_accounts",
    "credentials",
    "sheet_snapshots",
    "posts",
    "variants",
    "variant_media",
    "variant_events",
    "publish_attempts",
    "media_assets",
    "renditions",
    "channel_settings",
    "dry_run_posts",  # the rehearsal ledger; not one of the plan's eleven
}


def test_applies_in_order_and_a_second_run_is_a_no_op(fresh_conninfo: str) -> None:
    first = apply_migrations(fresh_conninfo, MIGRATIONS)
    assert first == sorted(first) and len(first) >= 3
    assert apply_migrations(fresh_conninfo, MIGRATIONS) == []


def test_creates_exactly_the_expected_tables(conninfo: str) -> None:
    with psycopg.connect(conninfo) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'publishing'"
            ).fetchall()
        }
    assert tables == EXPECTED_TABLES | {"schema_migrations"}


def test_editing_an_applied_migration_is_refused(tmp_path: Path, fresh_conninfo: str) -> None:
    shutil.copytree(MIGRATIONS, tmp_path / "m")
    apply_migrations(fresh_conninfo, tmp_path / "m")
    first = next((tmp_path / "m").glob("0001_*.sql"))
    first.write_text(first.read_text() + "\n-- sneaky edit\n")
    with pytest.raises(MigrationError, match="edited after it was applied"):
        apply_migrations(fresh_conninfo, tmp_path / "m")


def test_an_unapplied_migration_before_an_applied_one_is_refused(
    tmp_path: Path, fresh_conninfo: str
) -> None:
    shutil.copytree(MIGRATIONS, tmp_path / "m")
    apply_migrations(fresh_conninfo, tmp_path / "m")
    (tmp_path / "m" / "0000_late.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="earlier migration is not"):
        apply_migrations(fresh_conninfo, tmp_path / "m")


def test_a_failing_migration_rolls_back_completely(tmp_path: Path, fresh_conninfo: str) -> None:
    (tmp_path / "0001_bad.sql").write_text("CREATE TABLE publishing_t (a int); SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        apply_migrations(fresh_conninfo, tmp_path)
    with psycopg.connect(fresh_conninfo) as conn:
        assert conn.execute("SELECT to_regclass('publishing_t')").fetchone() == (None,)
        assert conn.execute("SELECT count(*) FROM publishing.schema_migrations").fetchone() == (0,)


def test_an_empty_directory_is_an_error(tmp_path: Path, fresh_conninfo: str) -> None:
    with pytest.raises(MigrationError, match="no migrations"):
        apply_migrations(fresh_conninfo, tmp_path)


# Schema conventions, enforced the way the AI harness enforces its own ----------------------------


def _scalar_list(conn: psycopg.Connection[tuple[object, ...]], sql: str) -> list[str]:
    return [str(r[0]) for r in conn.execute(sql).fetchall()]


def test_schema_conventions(conninfo: str) -> None:
    with psycopg.connect(conninfo) as conn:
        varchar = _scalar_list(
            conn,
            """SELECT table_name || '.' || column_name FROM information_schema.columns
               WHERE table_schema = 'publishing' AND data_type = 'character varying'""",
        )
        bare_timestamps = _scalar_list(
            conn,
            """SELECT table_name || '.' || column_name FROM information_schema.columns
               WHERE table_schema = 'publishing' AND data_type = 'timestamp without time zone'""",
        )
        enums = _scalar_list(
            conn,
            """SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
               WHERE n.nspname = 'publishing' AND t.typtype = 'e'""",
        )
        unnamed_pks = _scalar_list(
            conn,
            """SELECT conrelid::regclass::text FROM pg_constraint
               WHERE contype = 'p' AND connamespace = 'publishing'::regnamespace
                 AND conname NOT LIKE 'pk\\_%'""",
        )
        no_tenant = _scalar_list(
            conn,
            """SELECT t.tablename FROM pg_tables t
               WHERE t.schemaname = 'publishing' AND t.tablename <> 'schema_migrations'
                 AND NOT EXISTS (SELECT 1 FROM information_schema.columns c
                     WHERE c.table_schema = 'publishing' AND c.table_name = t.tablename
                       AND c.column_name = 'tenant_id')""",
        )
        tenantless_uniques = _scalar_list(
            conn,
            """SELECT c.conname FROM pg_constraint c
               WHERE c.contype = 'u' AND c.connamespace = 'publishing'::regnamespace
                 AND NOT EXISTS (SELECT 1 FROM pg_attribute a
                     WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                       AND a.attname = 'tenant_id')""",
        )
    assert varchar == [], "use text, not varchar"
    assert bare_timestamps == [], "use timestamptz, not timestamp"
    assert enums == [], "use text + CHECK, never native ENUM"
    assert unnamed_pks == [], "primary keys must be named pk_*"
    assert no_tenant == [], "every table carries tenant_id"
    assert tenantless_uniques == [], "every unique key includes tenant_id"


def test_sql_checks_agree_with_the_domain_enums(conninfo: str) -> None:
    """The CHECK lists are written out in SQL; this fails the moment they drift from the domain."""
    with psycopg.connect(conninfo) as conn:

        def check(name: str) -> str:
            row = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s", (name,)
            ).fetchone()
            assert row is not None, name
            return str(row[0])

        status_sql = check("ck_variants_status")
        action_sql = check("ck_variants_next_action")
    for status in VariantStatus:
        assert f"'{status.value}'" in status_sql
    assert status_sql.count("'") == 2 * len(VariantStatus)
    for action in Action:
        assert f"'{action.value}'" in action_sql
    assert action_sql.count("'") == 2 * len(Action)
