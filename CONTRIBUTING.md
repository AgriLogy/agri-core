# Contributing to agri-core

`agri-core` is the Agrilogy backend's **framework-agnostic business-logic library**: endpoint
handlers as plain functions, the FAO-56 / ASCE agronomy math, the alert evaluator + sensor-key
registry, and the SQLAlchemy DB-access layer over the `agri-db` models.

## 1. Where this repo sits

One-way dependency chain — never the reverse:

```
agri-api  ───▶  agri-core  ───▶  agri-db
(Django + FastAPI      (this repo:            (Postgres schema-of-record:
 HTTP shell, Celery,    handlers, agronomy,    Alembic migrations +
 device ingest views)   alerts, DB client)     SQLAlchemy ORM models)
```

**Hard rules** (`README.md`, `CLAUDE.md`, `src/agri/core/__init__.py`):

| Rule | Detail |
|---|---|
| No web framework | Zero `django`, `rest_framework`, `fastapi`, `celery` imports. A consumer view is a 3-line wrapper around a handler. |
| SQLAlchemy only | `agri.core.database` (`AgriMainDBClient` + `session_scope`) is the sanctioned DB path, over the ORM models `agri-db` owns. agri-core **owns DB access**; agri-api does not hand-roll queries. |
| Namespace package | `src/` layout, package is the namespace pair `agri.core` — imports read `from agri.core import ...`, mirroring `from agri.db import ...`. |
| Import must be DB-free | The engine is built lazily from `AGRI_DB_URL` on first use; importing any module must never require a live Postgres. |

## 2. Prerequisites & first-time setup

- Python **3.12+** (`requires-python = ">=3.12"`)
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — the Makefile hard-fails without it
- A GitHub PAT with **Contents: Read** on `AgriLogy/agri-db` — `agri-db` is private and pinned as a
  tagged git dependency, so `uv sync` cannot resolve without git credentials. CI uses the
  `AGRI_DB_RO_TOKEN` secret.

```bash
# one-time: let git fetch the private agri-db over HTTPS
git config --global \
  url."https://x-access-token:<YOUR_PAT>@github.com/".insteadOf "https://github.com/"

cd agri-core
make bootstrap          # uv sync --frozen (falls back to uv sync)
pre-commit install --install-hooks        # pre-commit + commit-msg stages
pre-commit install --hook-type pre-push   # lint + pytest + release-config gate
```

Co-developing against an **unreleased** agri-db: `uv pip install -e ../agri-db` (documented in
`pyproject.toml`). Revert before opening a PR — the tag pin is what CI resolves.

## 3. Dev loop

```bash
make lint           # uv run ruff check .
make format         # uv run ruff format .   (writes)
make format-check   # uv run ruff format --check .
make test           # uv run pytest -v       (125 tests today)
make all            # lint + format-check + test  ← run this before pushing
```

Ruff config: `target-version = py312`, `line-length = 100`, rules `E,F,W,I,UP,B,SIM`,
isort first-party `agri.core` + `agri.db`.

**Typecheck: not documented in-repo.** There is no mypy/pyright config, no `make typecheck`, and no
type job in CI. The package ships `py.typed`, so keep annotations accurate anyway.

## 4. Repo layout

| Path | Contents |
|---|---|
| `src/agri/core/__init__.py` | Re-exports `__version__`; documents the design rules. |
| `src/agri/core/_version.py` | Single version literal, **owned by python-semantic-release — never hand-edit**. |
| `src/agri/core/agronomy.py` | FAO-56 / ASCE math: Penman-Monteith hourly ET₀, VPD, net radiation, Kc/ETc, depletion + `irrigation_decision_dr`, `compute_zone_et0`, `field_snapshot*`. |
| `src/agri/core/alerts.py` | `SENSOR_KEY_REGISTRY`, `evaluate`, `AlertSpec`/`evaluate_alert`, `LatestReading`, `suggested_alert_payload`, DB-backed `recent_triggers_for_user` / `suggest_alert_for`. |
| `src/agri/core/notifications.py` | `compose_notification_email` (pure) + `compose_notification_for_user` (DB-backed). |
| `src/agri/core/et_forecast.py` | `DailyWeatherForecast`, `daily_et0_mm`, `et0_forecast`. |
| `src/agri/core/database/config.py` | DSN resolution: `AGRI_DB_URL` → `DATABASE_URL`, normalised to `postgresql+psycopg://`; `DatabaseNotConfiguredError`. |
| `src/agri/core/database/session.py` | Lazy process-wide engine (`NullPool`, `prepare_threshold=None` for the pgBouncer pooler), `session_scope`, `dispose_engine`. |
| `src/agri/core/database/client.py` | `AgriMainDBClient` — static query helpers (`get`, `list_`, `average_value`, `hourly_averages`, `latest_reading`, …) plus the device-JOIN ownership helpers `_owner_user` / `_owner_zone` / `_with_device_join`. |
| `tests/` | Flat pytest modules; `*_db.py` files are the DB-backed ones. |
| `docs/INDEX.md` | Doc index (mostly a stub today). |

### Device adapters

The design intent (README + module docstrings) is that device adapters (Bivocom, LoRaWAN/ChirpStack)
live here. **Today no adapter module exists in `src/`** — only the framework-agnostic half does
(`SENSOR_KEY_REGISTRY` in `alerts.py`, which maps a sensor key → unit, French label, type, and the
*name* of the storing model). The framework-coupled ingest halves still live in
`agri-api/back/src/fastapp/{ingest,mqtt}.py`.

So when you add one: create `src/agri/core/devices/<vendor>.py` with a **class-based adapter** — a
class per device family exposing a normalise/parse method that turns a raw vendor payload into
plain DTOs (dataclass or pydantic), zero HTTP/framework objects in or out. Register it in a
module-level registry keyed by vendor so agri-api resolves by string, exactly as
`SENSOR_KEY_REGISTRY` resolves model *names* rather than classes. Keep transport (webhook parsing,
auth, Celery dispatch) in agri-api.

## 5. Worked example — adding a capability end-to-end

Adding a DB-backed handler, e.g. `zone_water_balance(session, zone_id)`:

1. **Schema first.** If it needs new columns/tables, land them in `agri-db` and release a tag there.
2. **Pure logic** → `src/agri/core/agronomy.py` (or a new module). No I/O, no ORM: inputs are a
   dataclass, output is a DTO/dict. Unit-test it in `tests/test_agronomy.py` style — plain values,
   no database.
3. **DB access** → add a `@staticmethod` on `AgriMainDBClient` if you need a new query primitive.
   Reading queries must go through `_with_device_join` + `_owner_user`/`_owner_zone` so ownership
   resolves via `analytics_device` (a device transfer is then a one-row update).
4. **DB-backed entry point** → a function taking an externally-managed `Session` (the caller owns
   the transaction), following `compute_et0_for_zone` / `field_snapshot_for_user`.
5. **Tests.** Pure math in `tests/test_<topic>.py`; the DB path in `tests/test_<topic>_db.py`,
   which builds the relevant subset of `agri.db` tables on **in-memory SQLite** via
   `AgriBase.metadata` (see `tests/test_agronomy_db.py` for the pattern). No Postgres needed.
6. **Bump the pin.** After this repo releases, in `agri-api/back/pyproject.toml` bump
   `agri-core @ git+https://github.com/AgriLogy/agri-core.git@<new tag>`, `uv sync`, and write the
   thin wrapper:

   ```python
   # agri-api — FastAPI router (or DRF view); the only framework code
   from agri.core.database import session_scope
   from agri.core.agronomy import zone_water_balance

   @router.get("/zones/{zone_id}/water-balance")
   def water_balance(zone_id: int):
       with session_scope() as session:
           return zone_water_balance(session, zone_id)
   ```

## 6. Consumption & versioning

- **Pinned by git tag, not a wheel.** agri-api depends on
  `agri-core @ git+https://github.com/AgriLogy/agri-core.git@<tag>`; agri-core in turn pins
  `agri-db @ git+…@<tag>`. Tag format is bare `{version}` (`0.22.0`, no `v`).
- **python-semantic-release** (`.github/workflows/release.yml`) runs on every push to `main`,
  reads Conventional Commits since the last tag, rewrites `src/agri/core/_version.py`, regenerates
  `CHANGELOG.md`, tags, and cuts a GitHub Release. The release commit carries `[skip ci]`.
  `major_on_zero = false`; `feat` → minor, `fix`/`perf` → patch. `workflow_dispatch` with
  `release_type: rc` cuts an `rc` prerelease off a `feat|fix|perf/*` branch.
- **Release order for a cross-repo change** — strictly downstream-last:

  ```
  1. agri-db    : merge migration + models  → tag X.Y.Z
  2. agri-core  : bump the agri-db pin to X.Y.Z, add logic → merge → tag A.B.C
  3. agri-api   : bump the agri-core pin to A.B.C, add the wrapper → merge → deploy
  ```

  Keep the `agri-db` pin **at or above the live schema head** so agri-api applies the current
  migration chain. Apply the agri-db migration in prod before agri-api ships code that needs it.

## 7. Branch & PR rules

- Branch off **`main`**: `feat/<slug>`, `fix/<slug>`, `chore/<slug>` (matching the existing history).
- **One dedicated, scope-matched issue per PR.** The PR body contains `Closes #N`. Both the issue
  and the PR are assigned to `mks-zakaria` — the `Auto Assign` workflow does this on open.
- **PR title must be a Conventional Commit** — enforced by `lint-pr-title.yml`
  (`amannn/action-semantic-pull-request`). Squash-merge turns the title into the commit
  semantic-release classifies, so a sloppy title means a wrong (or missing) version bump.
  Allowed types: `feat fix perf refactor docs style test build ci chore revert`.
  Subject must start with a letter and not end with a period, e.g.
  `feat(handlers): add field_snapshot handler`.
- Individual commits are also checked by `conventional-pre-commit --strict` at `commit-msg`.
- **Zero AI/assistant attribution** anywhere: no `Co-Authored-By: Claude`, no "generated by"
  footers, no assistant names in commit messages, PR titles/bodies, issues, or branch names.
  The author is `MKS~ZAK <60817481+mks-zakaria@users.noreply.github.com>`.
- Commit from your local machine only — never over SSH on the droplet.

## 8. CI

`.github/workflows/` — all defined in-repo; **no reusable workflows are pulled from
`AgriLogy/shared-workflows`** in this repo.

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | PR + push to `main` | Configures the `AGRI_DB_RO_TOKEN` git credential, installs uv **0.11.6** (older uv can't parse this `uv.lock`), `uv python install 3.12`, `uv sync --frozen \|\| uv sync`, `ruff check .`, `pytest -v`. |
| `lint-pr-title.yml` | PR opened/edited/synchronized | Conventional-Commit validation of the PR title. |
| `auto-assign.yml` | issue/PR opened | Assigns `mks-zakaria`. |
| `release.yml` | push to `main`, `workflow_dispatch` | python-semantic-release v9.21.0 → version bump, changelog, tag, GitHub Release. |

Reproduce CI locally with `make all` (CI runs no `format-check`, but the pre-push hook and
`make all` do — keep formatting clean).

## 9. Gotchas

- **Private dependency.** Without a PAT-backed git credential, `uv sync` fails on `agri-db`.
  That is the #1 first-run failure.
- **`_version.py` is machine-owned.** Editing it by hand desynchronises the tag, the changelog and
  the pin downstream.
- **Tag pins don't float.** Merging to `main` changes nothing for agri-api until you bump its pin
  to the newly cut tag. `@main` is never used.
- **Tests use in-memory SQLite, not Postgres.** The `agri.db` models used in tests stick to portable
  column types, so `*_db.py` tests build the needed subset of tables on SQLite. If you add a query
  using a Postgres-only construct, the SQLite harness will not cover it — say so in the PR.
  (The dual-ORM Postgres-only test harness lives in **agri-api**, not here.)
- **Dual-ORM coexistence.** The live database is shared with agri-api's remaining Django ORM
  models. SQLAlchemy models here are the same physical tables — never assume exclusive ownership,
  and never let a schema assumption diverge from `agri-db`'s Alembic head.
- **Pooler settings are deliberate.** `NullPool` + `prepare_threshold=None` exist because pgBouncer
  in transaction mode owns pooling and cannot carry server-side prepared statements. Don't "fix"
  them by adding a SQLAlchemy pool.
- **Ownership resolution.** Reading queries resolve user/zone through the LEFT JOIN on
  `analytics_device` (`COALESCE(device.owner, row.owner)`). A new reading query that ignores this
  silently breaks device transfer history.
- **`.claude/COMMON_MISTAKES.md`, `QUICK_START.md`, `ARCHITECTURE_MAP.md` are unfilled templates** —
  they contain no real content despite what `CLAUDE.md` implies. This file is the actual guide.
