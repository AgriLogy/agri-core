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
* DB access lives in ``agri.core.database`` (Revly-style): handlers
  fetch through ``AgriMainDBClient`` over the ORM models that the
  sibling ``agri-db`` package owns. SQLAlchemy is allowed here;
  Django/DRF still are not.
"""

from __future__ import annotations

from agri.core._version import __version__

__all__ = ["__version__"]
