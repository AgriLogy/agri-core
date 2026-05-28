# agri-core

Framework-agnostic business logic and HTTP endpoint handlers for the
Agrilogy backend. Imported as a module by `agri-api` today; usable by a
future FastAPI service or a standalone ingest worker without
modification.

## Design rules

- Endpoint handlers live here as plain Python functions/classes.
  **No Django imports, no DRF imports.** The DRF view in `agri-api`
  becomes a 3-line wrapper:

  ```python
  # agri-core
  def get_active_zones(user_id: int, repo: ZonesRepository) -> list[ZoneDTO]: ...

  # agri-api
  class ActiveZonesView(APIView):
      def get(self, request):
          zones = get_active_zones(request.user.id, ZoneRepository())
          return Response(ActiveZonesSerializer(zones, many=True).data)
  ```

- Business logic (FAO-56 ET₀ / Kc / irrigation planning), device
  adapters (Bivocom, LoRaWAN/ChirpStack), and the alert evaluator live
  here too.
- Repository / service abstractions are injected by the caller — no
  ORM access inside this package. ORM models stay in `agri-db`
  (SQLAlchemy) and `agri-api` (Django) for now.

## Layout

```
agri-core/
├── pyproject.toml
├── Makefile
├── src/agri/core/        # namespace pair `agri.core`
│   └── __init__.py       # __version__
└── tests/
    └── test_smoke.py
```

The package is the namespace pair `agri.core`, matching `agri.db`
in the sibling [agri-db](../agri-db) repo. Downstream code imports
`from agri.core import ...`.

## Local development

```bash
make bootstrap     # uv sync (creates .venv, installs dev deps)
make lint          # ruff check
make test          # pytest
```

## Consuming from `agri-api`

Pin as a git dependency in `agri-api/back/pyproject.toml`:

```toml
dependencies = [
    ...,
    "agri-core @ git+https://github.com/AgriLogy/agri-core.git@<tag-or-sha>",
]
```

Run `uv sync` in `agri-api/back/` to install.

## Status

Scaffold only. Handler / business-logic extraction is happening in
`agri-api` PRs that lift modules out of `back/analytics/`,
`back/apps/*/`, and `back/agriBack/agronomy.py` one at a time. See the
senior-dev refactor playbook for the rolling phase list.
