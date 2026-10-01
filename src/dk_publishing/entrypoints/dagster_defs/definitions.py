from __future__ import annotations

from dagster import Definitions, EnvVar

from dk_publishing.entrypoints.dagster_defs.jobs import (
    expire_variant_job,
    housekeeping,
    prepare_variant_job,
    publish_variant_job,
    reconcile_variant_job,
)
from dk_publishing.entrypoints.dagster_defs.resources import ServicesResource
from dk_publishing.entrypoints.dagster_defs.schedules import housekeeping_schedule
from dk_publishing.entrypoints.dagster_defs.sensors import due_actions


def build_definitions(services: ServicesResource) -> Definitions:
    return Definitions(
        jobs=[
            prepare_variant_job,
            publish_variant_job,
            reconcile_variant_job,
            expire_variant_job,
            housekeeping,
        ],
        sensors=[due_actions],
        schedules=[housekeeping_schedule],
        resources={"services": services},
    )


defs = build_definitions(ServicesResource(database_url=EnvVar("DATABASE_URL")))
