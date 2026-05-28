"""agri.core — framework-agnostic business logic for the Agrilogy backend.

Imported by ``agri-api`` today; usable by a future FastAPI service or
standalone ingest worker without modification.

Design rules (see ``CLAUDE.md`` and the project memory entry
``project_agri_core_architecture``):

* Endpoint handlers live here as plain Python functions/classes.
  **No Django imports, no DRF imports.** The DRF view in agri-api is a
  3-line wrapper that unpacks the request and calls the handler.
* Business logic (FAO-56 / Kc / irrigation), device adapters, and the
  alert evaluator live here too.
* Repository/service abstractions are injected by the caller — no DB
  access inside this package.
"""
from __future__ import annotations

__version__ = "0.4.0"

__all__ = ["__version__"]
