from __future__ import annotations

import uuid
from datetime import timedelta

import psycopg
import pytest

from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.application.ports import DuplicateAttempt
from dk_publishing.domain.attempt import Outcome, Phase
from dk_publishing.domain.model import Variant
from dk_publishing.entrypoints.cli import main
from tests.integration.conftest import NOW, TENANT, Seed


def committed_variant(conninfo: str, seed: Seed) -> Variant:
    post_id, account_id = seed.post_and_account()
    variant = Variant(
        id=str(uuid.uuid4()),
        tenant_id=TENANT,
        post_id=post_id,
        platform="p",
        account_id=account_id,
        publish_at=NOW,
    )
    with PostgresUnitOfWork(conninfo) as uow:
        uow.variants.add(variant)
        uow.commit()
    return variant


def begin(uow: PostgresUnitOfWork, v: Variant, key: str = "k1") -> str:
    return uow.attempts.begin(
        tenant_id=v.tenant_id,
        variant_id=v.id,
        phase=Phase.PUBLISH,
        idempotency_key=key,
        started_at=NOW,
    )


def test_intent_is_logged_then_completed(conninfo: str, seed: Seed) -> None:
    v = committed_variant(conninfo, seed)
    with PostgresUnitOfWork(conninfo) as uow:
        attempt = begin(uow, v)
        uow.commit()  # the intent is durable before any platform call
    with PostgresUnitOfWork(conninfo) as uow:
        uow.attempts.finish(
            attempt,
            outcome=Outcome.REJECTED,
            finished_at=NOW + timedelta(seconds=2),
            error_code="400",
            http_status=400,
            response_excerpt="x" * 5000,
        )
        uow.commit()
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            "SELECT outcome, http_status, char_length(response_excerpt) FROM publishing.publish_attempts"
        ).fetchone()
    assert row == ("rejected", 400, 2000)


def test_reusing_an_idempotency_key_is_refused_without_poisoning_the_transaction(
    conninfo: str, seed: Seed
) -> None:
    v = committed_variant(conninfo, seed)
    with PostgresUnitOfWork(conninfo) as uow:
        begin(uow, v)
        uow.commit()
    with PostgresUnitOfWork(conninfo) as uow:
        with pytest.raises(DuplicateAttempt):
            begin(uow, v)
        assert begin(uow, v, "k2")  # the same transaction is still usable


def test_a_finished_attempt_is_immutable_and_attempts_cannot_be_deleted(
    conninfo: str, seed: Seed
) -> None:
    v = committed_variant(conninfo, seed)
    with PostgresUnitOfWork(conninfo) as uow:
        attempt = begin(uow, v)
        uow.attempts.finish(attempt, outcome=Outcome.OK, finished_at=NOW)
        uow.commit()
    with PostgresUnitOfWork(conninfo) as uow, pytest.raises(psycopg.errors.RestrictViolation):
        uow.attempts.finish(attempt, outcome=Outcome.REJECTED, finished_at=NOW)
    with pytest.raises(psycopg.errors.RestrictViolation), psycopg.connect(conninfo) as conn:
        conn.execute("DELETE FROM publishing.publish_attempts")


def test_finishing_an_unknown_attempt_is_an_error(conninfo: str) -> None:
    with PostgresUnitOfWork(conninfo) as uow, pytest.raises(LookupError):
        uow.attempts.finish(str(uuid.uuid4()), outcome=Outcome.OK, finished_at=NOW)


def test_cli_migrate(fresh_conninfo: str, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATABASE_URL", fresh_conninfo)
    assert main(["migrate"]) == 0
    assert "applied 0001_accounts.sql" in capsys.readouterr().out
    assert main(["migrate"]) == 0
    assert "already up to date" in capsys.readouterr().out


def test_cli_requires_database_url(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["migrate"]) == 2
    assert "DATABASE_URL is required" in capsys.readouterr().err
