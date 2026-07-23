# Onboarding — agri-core

## What this repo is

`agri-core` is the **framework-agnostic business-logic library** of the
Agrilogy backend, in the middle of the `agri-api → agri-core → agri-db`
chain:

- **agri-api** (Django/ninja shell) calls into agri-core handlers.
- **agri-core** owns the domain logic (agronomy, alerts, notifications,
  ET0/forecast, sensor handling) and the SQLAlchemy database access layer.
- **agri-db** (private repo) owns the schema: SQLAlchemy models + Alembic
  migrations. agri-core depends on it via a **tagged git URL**.

No web framework here — plain Python + SQLAlchemy, consumed as a
tag-pinned git dependency by agri-api.

## Local setup

```bash
uv sync          # or: make bootstrap
```

The private `agri-db` dependency must be fetchable. Either:

- SSH access to `AgriLogy/agri-db`, or
- a read-only PAT (Contents: Read on AgriLogy/agri-db) wired as a git
  credential:

  ```bash
  git config --global \
    url."https://x-access-token:${AGRI_DB_RO_TOKEN}@github.com/".insteadOf \
    "https://github.com/"
  ```

## Make targets

| Target | Does |
| --- | --- |
| `make bootstrap` | First-time setup: venv + dev deps (`uv sync`) |
| `make sync` / `make install` | Resolve + install deps from the lockfile |
| `make lint` | `ruff check` |
| `make format` | `ruff format` (writes) |
| `make format-check` | `ruff format --check` (read-only) |
| `make test` | pytest |
| `make all` | lint + format-check + test (what CI runs) |

## Contributing rules

- **Conventional commits** — squash-merge uses the PR title as the release
  commit, so PR titles must be conventional too (CI gates this).
- **PR ↔ issue pairing** — every PR opens with `Closes #N` on a dedicated,
  scope-matched issue; both assigned to `mks-zakaria`.

## Releases

`python-semantic-release` on push to `main`: computes the next version
from conventional commits, rewrites `src/agri/core/_version.py`, updates
`CHANGELOG.md`, and tags with **bare tags** (`0.18.2`, no `v` prefix).
Consumers (agri-api) pin those tags in their git dependency.

## CI

All workflows are thin callers into
[`AgriLogy/shared-workflows`](https://github.com/AgriLogy/shared-workflows)
pinned at `@v1`:

- `primary.yml` → `python-lint.yml` + `python-test.yml` (needs the
  `AGRI_DB_RO_TOKEN` repo secret for the private agri-db dependency)
- `release.yml` → `release.yml`
- `lint-pr-title.yml`, `auto-assign.yml` → same-named shared workflows
