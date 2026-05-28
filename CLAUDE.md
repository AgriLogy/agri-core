# CLAUDE.md — agri-core

Quick-start guide for Claude Code. **Read this in full; everything else is
on-demand.**

## What this repo is

The Agrilogy backend's framework-agnostic shared library. Holds
business logic and endpoint handlers. Consumed by `agri-api` today;
usable by a future FastAPI service or ingest worker.

**Tech:** Python 3.12 · pydantic · uv · setuptools (src/ layout)

## Sibling repos

| Repo | Path | Role |
|---|---|---|
| `agri-api` | `../agri-api/` | HTTP API service (Django+DRF). Pins `agri-core` as a git dep. |
| `agri-db` | `../agri-db/` | Postgres schema-of-record (Alembic + SQLAlchemy). |
| `agri-front` | `../agri-front/` | Web app. |

## ⚠ Read first

Three things that will bite if you skip them:

1. **No Django, no DRF imports here.** Per memory
   `project_agri_core_architecture`, agri-core must stay
   framework-agnostic so future consumers (FastAPI, ingest workers)
   can use it unchanged. The DRF wrapper lives in agri-api.
2. **Namespace package `agri.core`.** Imports are
   `from agri.core import ...`, mirroring `from agri.db import ...`.
   `src/` layout — code goes in `src/agri/core/`.
3. **Commit rules:** local machine only (never SSH); no
   `Co-Authored-By` trailer; every PR pairs with an issue; use
   `mks-zakaria` gh account. Stored in user memory.

## Quick commands

```bash
make bootstrap     # uv sync (creates .venv, installs dev deps)
make lint          # ruff check
make format        # ruff format
make test          # pytest
make all           # lint + format-check + test
```

## For architecture questions

The senior-dev skill at `~/.claude/skills/senior-dev/` holds the
multi-repo refactor playbook. Invoke via `/senior-dev` for
architecture or refactor questions.
