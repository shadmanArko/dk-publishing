"""A real Postgres for integration tests.

Order of preference:
1. `TEST_DATABASE_URL` (CI): an admin connection to an existing server.
2. A throwaway local cluster built with initdb (needs PostgreSQL binaries on the machine).
Without either, tests skip locally and fail under CI, so CI can never go green by skipping.
"""

from __future__ import annotations

import glob
import itertools
import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from dk_publishing.adapters.persistence.migrate import apply_migrations

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_names = itertools.count()


def _find_initdb() -> Path | None:
    candidates = [
        shutil.which("initdb"),
        *sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin/initdb"), reverse=True),
        *sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"), reverse=True),
    ]
    return next((Path(c) for c in candidates if c), None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def admin_conninfo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return

    initdb = _find_initdb()
    if initdb is None:
        message = "no Postgres available: set TEST_DATABASE_URL or install PostgreSQL"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    data = tmp_path_factory.mktemp("pgdata")
    port = _free_port()
    pg_ctl = initdb.with_name("pg_ctl")
    subprocess.run(
        [str(initdb), "-D", str(data), "-U", "postgres", "-A", "trust", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories= -c fsync=off"
    subprocess.run(
        [str(pg_ctl), "-D", str(data), "-o", options, "-w", "-l", str(data / "log"), "start"],
        check=True,
        capture_output=True,
    )
    try:
        yield f"host=127.0.0.1 port={port} user=postgres dbname=postgres"
    finally:
        subprocess.run([str(pg_ctl), "-D", str(data), "-m", "immediate", "stop"], check=False)


@pytest.fixture(scope="session")
def template_database(admin_conninfo: str) -> Iterator[str]:
    """Migrate once; every test then gets a cheap copy."""
    name = f"dk_template_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    apply_migrations(make_conninfo(admin_conninfo, dbname=name), MIGRATIONS)
    yield name
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def conninfo(admin_conninfo: str, template_database: str) -> Iterator[str]:
    name = f"dk_test_{next(_names)}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_database}"')
    yield make_conninfo(admin_conninfo, dbname=name)
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def fresh_conninfo(admin_conninfo: str) -> Iterator[str]:
    """An empty database, for testing the migration runner itself."""
    name = f"dk_fresh_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(admin_conninfo, dbname=name)
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TENANT = "dk"


class Seed:
    """Inserts the rows a variant needs to exist (posts and accounts have no repository yet)."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def post_and_account(self, post_key: str = "DK-2026-0001") -> tuple[str, str]:
        with psycopg.connect(self.conninfo) as conn:
            post = conn.execute(
                "INSERT INTO publishing.posts (tenant_id, post_key) VALUES (%s, %s) RETURNING id::text",
                (TENANT, post_key),
            ).fetchone()
            account = conn.execute(
                """INSERT INTO publishing.social_accounts (tenant_id, platform, external_id)
                   VALUES (%s, 'p', %s) RETURNING id::text""",
                (TENANT, uuid.uuid4().hex),
            ).fetchone()
        assert post and account
        return str(post[0]), str(account[0])


@pytest.fixture
def seed(conninfo: str) -> Seed:
    return Seed(conninfo)
