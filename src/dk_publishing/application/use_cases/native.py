from __future__ import annotations

from datetime import datetime

from dk_publishing.application.ports import NativeScheduler
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import S
from dk_publishing.domain.errors import IntegrityError
from dk_publishing.domain.model import Variant


def withdraw_native(services: Services, variant: Variant, now: datetime) -> bool:
    """Take a natively scheduled post back from the platform so it will not publish.

    Returns True when it is gone (or there was nothing scheduled). Returns False, touching
    nothing, once the slot has arrived: the platform may already have published it, and a
    published post is out of scope for cancelling. The next reconciliation settles that case.
    Any platform error propagates, leaving the variant as it was, so the caller can try again.
    """
    if variant.status is not S.SCHEDULED_NATIVE:
        return True
    if now >= variant.publish_at:
        return False
    publisher = services.publishers.for_platform(variant.platform)
    if not isinstance(publisher, NativeScheduler):
        raise IntegrityError(f"{variant.platform} holds a scheduled post but cannot cancel it")
    with services.uow() as uow:
        handle = uow.variants.handle_of(variant.id)
    if handle is None:
        raise IntegrityError(f"scheduled variant {variant.id} has no handle to cancel")
    publisher.cancel(handle)
    return True
