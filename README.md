# dk-publishing

Schedules approved Dhaka Kacchi posts to Facebook, Instagram, Threads, YouTube, TikTok, LinkedIn, X and Reddit
from one Google Sheet. Dagster is the only clock, Postgres is the only source of truth.

Start with [`docs/architecture.md`](docs/architecture.md), then [`docs/adr/`](docs/adr/README.md).

```bash
make install   # uv sync
make check     # ruff, mypy --strict, import-linter, pytest + 90% coverage gate
```
