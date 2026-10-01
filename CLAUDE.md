# Working context — dk-publishing

`docs/architecture.md` is the map; `docs/adr/` records why. This file lists what will bite you.

- **Dependency rule:** `domain` imports only the stdlib; `application` only `domain`; only `composition.py`
  constructs adapters; nothing imports `entrypoints`. Enforced by `make arch` and `tests/unit/test_architecture.py`.
- **Platform names** (instagram, facebook, ...) appear only in `adapters/platforms/` and `config/`. The scheduler
  reads `Capabilities`, never branches on a platform.
- **Never post twice.** No blind retry after a sent publish request. Uncertain outcome -> `unknown` -> reconcile.
- **Only `adapters/secrets` imports `cryptography`.** No LLM SDK is ever imported here.
- **Times:** UTC `timestamptz` in storage; Europe/Berlin only in the Sheet and messages. Reject DST-ambiguous times.
- **Separate from the harness repo** on purpose (ADR 0013). Never run anything that downgrades/reset this schema
  against a database holding live tokens or publish state.
- Run `make check` before every commit.
