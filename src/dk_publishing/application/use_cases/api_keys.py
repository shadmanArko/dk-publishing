"""API keys: made once, shown once, stored only as a hash."""

from __future__ import annotations

import hashlib
import secrets

from dk_publishing.application.ports import ApiKey, ApiKeyInfo
from dk_publishing.application.services import Services

PREFIX = "dkp_"
READ, DRAFT, APPROVE = "read", "draft", "approve"
SCOPES = (READ, DRAFT, APPROVE)  # approve implies draft implies read; see `allows`
RANK = {READ: 0, DRAFT: 1, APPROVE: 2}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def allows(key: ApiKey, needed: str) -> bool:
    """A key's strongest scope covers the weaker ones: approve > draft > read."""
    return max((RANK[s] for s in key.scopes if s in RANK), default=-1) >= RANK[needed]


def create_key(services: Services, tenant_id: str, name: str, scope: str) -> str:
    """Returns the key itself. It cannot be recovered later."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of: {', '.join(SCOPES)}")
    key = PREFIX + secrets.token_urlsafe(32)
    with services.uow() as uow:
        uow.api.add_key(tenant_id, name, hash_key(key), [scope])
        uow.commit()
    return key


def authenticate(services: Services, token: str) -> ApiKey | None:
    if not token.startswith(PREFIX):
        return None
    with services.uow() as uow:
        found = uow.api.key_by_hash(hash_key(token))
        if found is not None:
            uow.api.touch_key(found.id, services.clock.now())
            uow.commit()
    return found


def list_keys(services: Services, tenant_id: str) -> list[ApiKeyInfo]:
    with services.uow() as uow:
        return uow.api.keys(tenant_id)


def revoke_key(services: Services, tenant_id: str, name: str) -> bool:
    with services.uow() as uow:
        done = uow.api.revoke_key(tenant_id, name, services.clock.now())
        uow.commit()
    return done
