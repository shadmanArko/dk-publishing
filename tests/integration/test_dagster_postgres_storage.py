"""Dagster's run history in a real Postgres, the way production configures it.

Found on the first deploy: with SQLAlchemy 2.1 a plain `postgresql://` URL selects the psycopg (v3)
driver and Dagster's event storage fails on `NOTIFY ... $1`, so no run could launch. Local runs use
SQLite and never saw it. This runs a job against real Postgres with the URL production uses.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from dagster import DagsterInstance, job, op
from psycopg.conninfo import conninfo_to_dict

REPO = Path(__file__).resolve().parents[2]


@op
def hello() -> int:
    return 1


@job
def hello_job() -> None:
    hello()


def _storage_url(conninfo: str, scheme: str) -> tuple[str, str]:
    parts = conninfo_to_dict(conninfo)
    name = f"dagster_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    password = f":{parts['password']}" if parts.get("password") else ""
    host, port = parts.get("host", "localhost"), parts.get("port", "5432")
    return f"{scheme}://{parts.get('user', 'postgres')}{password}@{host}:{port}/{name}", name


def _run(url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    (tmp_path / "dagster.yaml").write_text(
        f"telemetry:\n  enabled: false\nstorage:\n  postgres:\n    postgres_url: {url}\n"
    )
    monkeypatch.setenv("DAGSTER_HOME", str(tmp_path))
    instance = DagsterInstance.get()
    try:
        return hello_job.execute_in_process(instance=instance, raise_on_error=False).success
    finally:
        instance.dispose()


def test_a_job_runs_against_postgres_with_the_production_driver(
    conninfo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = _storage_url(conninfo, "postgresql+psycopg2")
    assert _run(url, tmp_path, monkeypatch)


def test_production_names_the_psycopg2_driver_in_both_places_that_matter() -> None:
    compose = (REPO / "deploy" / "compose.prod.yaml").read_text()
    assert re.search(r"DAGSTER_DATABASE_URL: postgresql\+psycopg2://", compose)
    prod = yaml.safe_load((REPO / "dagster" / "dagster.prod.yaml").read_text())
    assert prod["storage"]["postgres"]["postgres_url"] == {"env": "DAGSTER_DATABASE_URL"}
    assert os.path.exists(REPO / "deploy" / "compose.prod.yaml")
