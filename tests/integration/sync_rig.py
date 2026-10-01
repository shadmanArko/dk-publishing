from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from dk_publishing.application.ports import SyncVariant
from dk_publishing.application.services import SyncServices
from dk_publishing.application.use_cases.sync_sheet import SyncReport, sync_sheet
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.timezones import utc_to_berlin
from tests.integration.conftest import TENANT, Seed
from tests.integration.rig import Rig
from tests.support import CAPS, T0
from tests.support.memory_sheet import FakeMedia, MemorySheet


def berlin_in(hours: float) -> datetime:
    """Berlin wall-clock time `hours` after the rig clock's start, the way a person types it."""
    return utc_to_berlin(T0 + timedelta(hours=hours)).replace(tzinfo=None)


class SyncRig(Rig):
    def __init__(
        self, conninfo: str, seed: Seed, *, caps: Capabilities = CAPS, **script: object
    ) -> None:
        super().__init__(conninfo, seed, caps=caps, **script)
        self.sheet = MemorySheet()
        self.media = FakeMedia("reel.mp4")
        self.sync_services = SyncServices(
            core=self.services,
            sheet=self.sheet,
            media=self.media,
            platforms=["p"],
            tenant_id=TENANT,
        )
        self.add_account("Main")

    def add_account(self, name: str) -> str:
        with self.services.uow() as uow:
            account_id = uow.sync.add_account(TENANT, "p", name, uuid.uuid4().hex)
            uow.commit()
        return account_id

    def sync(self, **kw: bool) -> SyncReport:
        return sync_sheet(self.sync_services, **kw)

    def post_with_row(
        self, key: str = "DK-1", *, ready: bool = True, slot_in: float = 5, **post: object
    ) -> None:
        """A Posts row plus one platform row, the way the README example describes."""
        self.sheet.add_post(
            key, default_slot=berlin_in(slot_in), ready=ready, media=("reel.mp4",), **post
        )
        self.sheet.add_row(key)

    def variants(self) -> list[SyncVariant]:
        with self.services.uow() as uow:
            return uow.sync.variants(TENANT)

    def only(self, key: str = "DK-1") -> SyncVariant:
        [found] = [v for v in self.variants() if v.post_key == key]
        return found

    def statuses(self) -> dict[str, str]:
        return {v.post_key: v.variant.status.value for v in self.variants()}
