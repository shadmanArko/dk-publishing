"""The Dagster shell around the use cases, run in-process against real Postgres."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from dagster import (
    DagsterInstance,
    Definitions,
    ExecuteInProcessResult,
    RunRequest,
    SkipReason,
    build_sensor_context,
)

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.entrypoints.dagster_defs import build_definitions, defs
from dk_publishing.entrypoints.dagster_defs.resources import ServicesResource
from tests.integration.conftest import Seed
from tests.integration.rig import CONTENT, ME, SLOT, R, Rig, S
from tests.support import MIN, SimulatedCrash

REPO = Path(__file__).resolve().parents[2]
_SERVICES: dict[str, Services] = {}


class FakeServicesResource(ServicesResource):
    """Same resource, but the use cases get the rig's fake clock and scripted publisher."""

    def services(self) -> Services:
        return _SERVICES[self.database_url]


def wire(rig: Rig) -> tuple[Definitions, ServicesResource]:
    _SERVICES[rig.conninfo] = rig.services
    resource = FakeServicesResource(database_url=rig.conninfo)
    return build_definitions(resource), resource


def evaluate(definitions: Definitions, resource: ServicesResource) -> list[Any]:
    sensor = definitions.get_sensor_def("due_actions")
    return list(sensor(build_sensor_context(resources={"services": resource})))  # type: ignore[arg-type]


def requests(definitions: Definitions, resource: ServicesResource) -> list[RunRequest]:
    return [r for r in evaluate(definitions, resource) if isinstance(r, RunRequest)]


def run(definitions: Definitions, request: RunRequest) -> ExecuteInProcessResult:
    assert request.job_name is not None
    job = definitions.get_job_def(request.job_name)
    return job.execute_in_process(
        run_config=request.run_config, instance=DagsterInstance.ephemeral()
    )


def test_every_expected_definition_is_present() -> None:
    names = {j.name for j in defs.jobs or []}
    assert names == {
        "prepare_variant",
        "publish_variant",
        "reconcile_variant",
        "schedule_native",
        "expire_variant",
        "housekeeping",
        "sync_sheet",
    }
    assert defs.get_sensor_def("due_actions").minimum_interval_seconds == 30
    assert defs.get_sensor_def("sheet_changed").minimum_interval_seconds == 120
    schedule = defs.get_schedule_def("housekeeping")
    assert (schedule.cron_schedule, schedule.execution_timezone) == ("*/5 * * * *", "Europe/Berlin")


def test_nothing_due_is_a_skip(conninfo: str, seed: Seed) -> None:
    definitions, resource = wire(Rig(conninfo, seed))
    [result] = evaluate(definitions, resource)
    assert isinstance(result, SkipReason)


def test_the_sensor_launches_one_keyed_run_per_due_action(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    variant = rig.approved()
    definitions, resource = wire(rig)

    assert isinstance(evaluate(definitions, resource)[0], SkipReason)  # prepare is not due yet
    rig.clock.set(SLOT - 30 * MIN)
    [request] = requests(definitions, resource)

    assert request.run_key == f"prepare:{variant.id}:{variant.version}"
    assert request.job_name == "prepare_variant"
    assert request.tags["dk/variant"] == variant.id
    assert request.tags["dk/platform"] == "p" and request.tags["dk/account"]
    # Evaluating again yields the same key, which is what lets the daemon launch it only once.
    assert requests(definitions, resource)[0].run_key == request.run_key


def test_a_run_executes_the_use_case(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    variant = rig.approved()
    definitions, resource = wire(rig)
    rig.clock.set(SLOT - 30 * MIN)

    [request] = requests(definitions, resource)
    assert run(definitions, request).success
    assert rig.get(variant.id).status is S.PREPARED

    rig.clock.set(SLOT)
    [request] = requests(definitions, resource)
    assert request.job_name == "publish_variant"
    assert run(definitions, request).success
    assert rig.get(variant.id).status is S.PUBLISHED and rig.posts(variant.id) == 1


def test_a_stale_run_is_a_success_that_does_nothing(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    variant = rig.approved()
    definitions, resource = wire(rig)
    rig.clock.set(SLOT - 30 * MIN)
    [request] = requests(definitions, resource)
    assert prepare_variant(rig.services, variant.id, variant.version) is R.PREPARED  # someone else

    assert run(definitions, request).success
    assert rig.get(variant.id).version == variant.version + 2  # approved -> preparing -> prepared


def test_housekeeping_recovers_runs_that_died(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed, publish=[("after", SimulatedCrash())])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    with pytest.raises(SimulatedCrash):
        publish_variant(rig.services, variant.id, variant.version)
    definitions, _ = wire(rig)

    rig.clock.advance(11 * MIN)
    assert (
        definitions.get_job_def("housekeeping")
        .execute_in_process(instance=DagsterInstance.ephemeral())
        .success
    )
    assert rig.get(variant.id).status is S.UNKNOWN

    [request] = requests(definitions, FakeServicesResource(database_url=rig.conninfo))
    assert request.job_name == "reconcile_variant"
    assert run(definitions, request).success
    assert rig.get(variant.id).status is S.PUBLISHED and rig.posts(variant.id) == 1


def test_a_day_of_posts_through_the_sensor_and_jobs(conninfo: str, seed: Seed) -> None:
    """The daemon, simulated: tick the sensor, launch each new run key once, advance the clock."""
    rig = Rig(conninfo, seed)
    definitions, resource = wire(rig)
    slots = [rig.clock.now() + timedelta(minutes=m) for m in (40, 70, 70, 95, 200)]
    ids = []
    for slot in slots:
        variant = rig.draft(publish_at=slot)
        assert approve(rig.services, variant.id, CONTENT, ME)[0] is R.APPROVED
        ids.append(variant.id)

    launched: set[str] = set()
    end = rig.clock.now() + timedelta(hours=6)
    while rig.clock.now() < end:
        for request in requests(definitions, resource):
            assert request.run_key is not None
            if request.run_key not in launched:
                launched.add(request.run_key)
                assert run(definitions, request).success
        rig.clock.advance(timedelta(seconds=30))

    for variant_id, slot in zip(ids, slots, strict=True):
        status, *_, published_at = rig.row(variant_id)
        assert status == "published" and rig.posts(variant_id) == 1
        assert timedelta(0) <= published_at - slot <= timedelta(minutes=1)  # type: ignore[operator]
    assert len(launched) == 2 * len(slots)  # one prepare and one publish each


def test_the_queue_allows_one_run_per_account_and_one_sync_at_a_time() -> None:
    for name in ("dagster.dev.yaml", "dagster.prod.yaml"):
        config = yaml.safe_load((REPO / "dagster" / name).read_text())
        limits = {
            x["key"]: x for x in config["run_coordinator"]["config"]["tag_concurrency_limits"]
        }
        assert limits["dk/account"]["limit"] == 1
        assert limits["dk/account"]["value"]["applyLimitPerUniqueValue"] is True
        assert limits["dk/sync"]["limit"] == 1
    prod = yaml.safe_load((REPO / "dagster" / "dagster.prod.yaml").read_text())
    assert prod["storage"]["postgres"]["postgres_url"] == {"env": "DAGSTER_DATABASE_URL"}


def test_build_services_wires_only_platforms_that_are_on(conninfo: str) -> None:
    services = composition.build_services(conninfo)
    assert services.publishers.for_platform("instagram").capabilities.native_window is None
    # Facebook keeps its native window; each row's `delivery` decides whether the planner uses it.
    assert services.publishers.for_platform("facebook").capabilities.native_window == (
        timedelta(minutes=10),
        timedelta(days=30),
    )
    from dk_publishing.adapters.platforms.registry import UnknownPlatform

    with pytest.raises(UnknownPlatform):
        services.publishers.for_platform("x")  # mode: off


def test_a_platform_without_an_adapter_stops_start_up(tmp_path: Path, conninfo: str) -> None:
    config = tmp_path / "platforms.yaml"
    config.write_text("platforms:\n  instagram: {mode: live}\n")
    with pytest.raises(ConfigError, match="no such adapter is built yet"):
        composition.build_services(conninfo, config)


def test_the_database_url_comes_from_the_environment(
    conninfo: str, monkeypatch: pytest.MonkeyPatch, seed: Seed
) -> None:
    """The real, unfaked resource builds working services from DATABASE_URL."""
    monkeypatch.setenv("DATABASE_URL", conninfo)
    post_id, account_id = seed.post_and_account("DK-ENV")
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            """INSERT INTO publishing.variants (tenant_id, post_id, platform, account_id, publish_at)
               VALUES ('dk', %s::uuid, 'instagram', %s::uuid, now())""",
            (post_id, account_id),
        )
    resolved = defs.get_job_def("housekeeping").execute_in_process(
        instance=DagsterInstance.ephemeral()
    )
    assert resolved.success


def test_rehearsal_seeding_covers_every_platform_that_is_on(conninfo: str) -> None:
    results = composition.seed_rehearsal(
        conninfo, count=2, spacing=timedelta(minutes=5), platforms=None
    )
    assert results == [R.APPROVED] * 12  # 2 posts x the 6 platforms that are not off
    with psycopg.connect(conninfo) as conn:
        platforms = {
            r[0] for r in conn.execute("SELECT DISTINCT platform FROM publishing.variants")
        }
    assert len(platforms) == 6 and not platforms & {"x", "reddit"}


def test_a_native_post_is_handed_over_by_its_own_job_and_published_by_the_platform(
    conninfo: str, seed: Seed
) -> None:
    from dk_publishing.application.use_cases.approve import approve
    from tests.support import NATIVE_CAPS

    rig = Rig(conninfo, seed, caps=NATIVE_CAPS)
    definitions, resource = wire(rig)
    variant = rig.draft()
    approve(rig.services, variant.id, {"caption": "Eid", "delivery": "native"}, ME)

    [request] = requests(definitions, resource)
    assert request.job_name == "schedule_native"
    assert request.run_key == f"schedule_native:{variant.id}:{rig.get(variant.id).version}"
    assert run(definitions, request).success
    assert rig.get(variant.id).status is S.SCHEDULED_NATIVE

    rig.clock.set(SLOT + 10 * MIN)  # the platform has published it; the next look confirms it
    [verify] = requests(definitions, resource)
    assert verify.job_name == "reconcile_variant" and run(definitions, verify).success
    assert rig.get(variant.id).status is S.PUBLISHED and rig.publisher.calls["publish"] == 0
