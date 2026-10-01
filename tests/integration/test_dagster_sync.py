"""The Dagster side of the Sheet sync: the sensor that notices edits, and the job."""

from __future__ import annotations

from typing import Any

import pytest
from dagster import DagsterInstance, RunRequest, SkipReason, build_sensor_context

from dk_publishing.application.services import SyncServices
from dk_publishing.entrypoints.dagster_defs import build_definitions, defs
from dk_publishing.entrypoints.dagster_defs.resources import ServicesResource, SheetSyncResource
from tests.integration.conftest import Seed
from tests.integration.sync_rig import SyncRig

_SYNC: dict[str, SyncServices] = {}
_MODIFIED: dict[str, str] = {}


class FakeSheetSync(SheetSyncResource):
    def services(self) -> SyncServices:
        return _SYNC[self.database_url]

    def modified_at(self) -> str:
        return _MODIFIED[self.database_url]


class FakeServices(ServicesResource):
    pass


def wire(rig: SyncRig, modified: str = "2026-10-01T10:00:00.000Z") -> tuple[Any, FakeSheetSync]:
    _SYNC[rig.conninfo] = rig.sync_services
    _MODIFIED[rig.conninfo] = modified
    resource = FakeSheetSync(
        database_url=rig.conninfo, credentials_path="-", sheet_id="-", folder_id="-"
    )
    return build_definitions(FakeServices(database_url=rig.conninfo), resource), resource


def evaluate(
    definitions: Any, resource: FakeSheetSync, cursor: str | None = None
) -> tuple[list[Any], Any]:
    sensor = definitions.get_sensor_def("sheet_changed")
    context = build_sensor_context(resources={"sheet_sync": resource}, cursor=cursor)
    return list(sensor(context)), context


@pytest.fixture
def rig(conninfo: str, seed: Seed) -> SyncRig:
    return SyncRig(conninfo, seed)


def test_the_first_look_starts_a_sync_tagged_so_only_one_runs_at_a_time(rig: SyncRig) -> None:
    definitions, resource = wire(rig)
    [request], context = evaluate(definitions, resource)
    assert isinstance(request, RunRequest)
    assert request.run_key == "sync:2026-10-01T10:00:00.000Z"
    assert request.tags["dk/sync"] == "sheet"
    assert context.cursor == "2026-10-01T10:00:00.000Z"


def test_an_unchanged_sheet_starts_nothing(rig: SyncRig) -> None:
    definitions, resource = wire(rig)
    [result], _ = evaluate(definitions, resource, cursor="2026-10-01T10:00:00.000Z")
    assert isinstance(result, SkipReason)


def test_an_edit_starts_a_new_sync_with_a_new_run_key(rig: SyncRig) -> None:
    definitions, resource = wire(rig)
    _MODIFIED[rig.conninfo] = "2026-10-01T10:05:00.000Z"
    [request], _ = evaluate(definitions, resource, cursor="2026-10-01T10:00:00.000Z")
    assert request.run_key == "sync:2026-10-01T10:05:00.000Z"


def test_the_sync_job_brings_the_sheet_into_postgres(rig: SyncRig) -> None:
    rig.post_with_row()
    definitions, _ = wire(rig)
    result = definitions.get_job_def("sync_sheet").execute_in_process(
        instance=DagsterInstance.ephemeral()
    )
    assert result.success
    assert rig.only().variant.status.value == "approved"
    assert rig.sheet.status("DK-1")["status"] == "approved"


def test_a_halted_sync_fails_the_run_so_a_person_notices(rig: SyncRig) -> None:
    for i in range(1, 8):
        rig.post_with_row(f"DK-{i}")
    rig.sync()
    for i in range(1, 8):
        rig.sheet.delete_rows(f"DK-{i}")
    definitions, _ = wire(rig)

    result = definitions.get_job_def("sync_sheet").execute_in_process(
        instance=DagsterInstance.ephemeral(), raise_on_error=False
    )

    assert not result.success
    failure = next(e for e in result.all_events if e.is_failure)
    assert "Nothing was changed" in str(failure.event_specific_data)
    assert set(rig.statuses().values()) == {"approved"}


def test_the_fallback_schedule_runs_the_sync_every_fifteen_minutes() -> None:
    schedule = defs.get_schedule_def("sheet_sync_fallback")
    assert (schedule.cron_schedule, schedule.execution_timezone) == (
        "*/15 * * * *",
        "Europe/Berlin",
    )
