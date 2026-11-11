# agri-core onboarding

The short version. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the full rules.

## What this repo is

The Agrilogy backend's **framework-agnostic business logic**. It is a plain
Python library (namespace package `agri.core`) that sits in the middle of the
backend's one-way dependency chain:

```
agri-api  ───▶  agri-core  ───▶  agri-db
(HTTP shell:     (THIS REPO:        (Postgres schema-of-record:
 Django + FastAPI, handlers, agronomy, SQLAlchemy models +
 Celery, ingest)   alerts, DB access) Alembic migrations)
```

- **agri-api** imports it and wraps each handler in a thin view.
- **agri-db** is a dependency of this repo, never the reverse.
- What lives here: endpoint handlers as plain functions, the FAO-56 / ASCE
  agronomy math, sensor calibration, the alert evaluator and sensor-key
  registry, notifications, and `agri.core.database` (the SQLAlchemy client over
  the agri-db models).
- No web framework imports (`django`, `rest_framework`, `fastapi`, `celery`),
  and importing any module must not need a live Postgres.

## Local setup

You need Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a GitHub PAT
with **Contents: Read** on `AgriLogy/agri-db`. agri-db is private and pinned
as a tagged git dependency (`agri-db @ git+https://github.com/AgriLogy/agri-db.git@<tag>`),
so `uv sync` cannot resolve it without git credentials. In CI the same PAT is
the `AGRI_DB_RO_TOKEN` repository secret.

```bash
git config --global \
  url."https://x-access-token:<YOUR_PAT>@github.com/".insteadOf "https://github.com/"

make bootstrap                            # uv sync: creates .venv with dev deps
pre-commit install --install-hooks        # pre-commit + commit-msg hooks
pre-commit install --hook-type pre-push   # lint + tests before push
```

To work against an **unreleased** agri-db, run `uv pip install -e ../agri-db`
and use `uv run --no-sync ...` so uv doesn't reinstall the pinned tag. Undo
this before opening a PR, because CI resolves the tag pin.

## Make targets

| Target | What it runs |
| --- | --- |
| `make bootstrap` / `make sync` | `uv sync` (lockfile install) |
| `make lint` | `uv run ruff check .` |
| `make format` | `uv run ruff format .` (writes) |
| `make format-check` | `uv run ruff format --check .` |
| `make test` | `uv run pytest -v` |
| `make all` | lint + format-check + test. Run it before pushing. |

## Contributing

- **Every PR is paired with an issue** (`Closes #N` in the body). Never push to
  `main`: branch, then PR, then squash-merge once CI is green.
- **The PR title is a Conventional Commit** (`feat(alerts): ...`,
  `fix(calibration): ...`). The squash commit uses the title, and the title
  decides the release.

## Releases

python-semantic-release runs on every push to `main`:

- `feat` → minor, `fix` / `perf` → patch, anything else → no release
  (`major_on_zero = false`).
- Tags are **bare** (`0.28.1`, not `v0.28.1`). agri-api pins
  `agri-core @ git+https://github.com/AgriLogy/agri-core.git@<tag>`.
- An agri-db upgrade therefore goes: release agri-db → bump the pin here in a
  `fix(deps)` PR (patch release) → bump agri-api's pin to the new agri-core tag.
- Release candidates: dispatch `release` with `release_type=rc` on a
  `feat/*`, `fix/*` or `perf/*` branch.

## CI layout

Every workflow is a thin caller of
[`AgriLogy/shared-workflows`](https://github.com/AgriLogy/shared-workflows) `@v1`:

| Workflow | Calls | Notes |
| --- | --- | --- |
| `primary.yml` (CI) | `python-lint.yml`, `python-test.yml` | ruff check + format check, then pytest. Both pull agri-db with `AGRI_DB_RO_TOKEN` (`secrets: inherit`). |
| `release.yml` | `release.yml` | The `[skip ci]` guard, the `release-<ref>` concurrency group and the `release_type` dispatch input stay in the caller. |
| `lint-pr-title.yml` | `lint-pr-title.yml` | Conventional Commit PR titles. |
| `auto-assign.yml` | `auto-assign.yml` | Assigns new issues/PRs to `mks-zakaria`. |

To change CI behaviour, change shared-workflows and move its `v1` tag. Only
triggers, permissions and inputs belong in this repo.
