# CHANGELOG


## v0.12.0 (2026-06-09)

### Features

- **database**: Generalize hourly_averages for the sensors endpoint
  ([#28](https://github.com/AgriLogy/agri-core/pull/28),
  [`641704f`](https://github.com/AgriLogy/agri-core/commit/641704f0d28896dbdeaba3e98cdeff01dadcfd87))

Closes #27


## v0.11.0 (2026-06-09)

### Features

- **database**: Add hourly_averages to AgriMainDBClient
  ([#26](https://github.com/AgriLogy/agri-core/pull/26),
  [`4510246`](https://github.com/AgriLogy/agri-core/commit/4510246dfe181c9bae292bb7e11a0912d631b201))

Closes #25


## v0.10.0 (2026-06-07)

### Features

- **alerts**: Add battery + signal sensor keys
  ([#24](https://github.com/AgriLogy/agri-core/pull/24),
  [`5698b22`](https://github.com/AgriLogy/agri-core/commit/5698b22f4462dfb07be6abcd735a159428af51b1))

Register two device-health metrics in SENSOR_KEY_REGISTRY: * battery — voltage (V), model
  BatterySensor * signal — RSSI (dBm), model SignalSensor

Both alert on LOW values (low battery / weak signal are the failure modes), like soil moisture.
  Bumps the agri-db pin to 0.2.0, which adds the matching AnalyticsBatterysensor /
  AnalyticsSignalsensor ORM models so db_model_for resolves the new keys.


## v0.9.0 (2026-05-30)

### Features

- **notifications**: Db-backed compose_notification_for_user
  ([#22](https://github.com/AgriLogy/agri-core/pull/22),
  [`c0374f1`](https://github.com/AgriLogy/agri-core/commit/c0374f11a99618725bb3a7f1deef479e99f39774))

Closes #21


## v0.8.0 (2026-05-30)

### Features

- **alerts**: Db-backed recent_triggers_for_user + suggest_alert_for
  ([#20](https://github.com/AgriLogy/agri-core/pull/20),
  [`73efd5b`](https://github.com/AgriLogy/agri-core/commit/73efd5b3f70b5285b29836454fb95bdcce72c695))

Closes #19


## v0.7.0 (2026-05-30)

### Features

- **agronomy**: Db-backed field_snapshot_for_user
  ([#18](https://github.com/AgriLogy/agri-core/pull/18),
  [`dcd04ce`](https://github.com/AgriLogy/agri-core/commit/dcd04ce3ce8cacf356414e6baddf6fd0106e68fa))

Closes #17


## v0.6.1 (2026-05-29)

### Bug Fixes

- **deps**: Pin agri-db@0.1.1 + add pre-push gate
  ([#16](https://github.com/AgriLogy/agri-core/pull/16),
  [`53b81c9`](https://github.com/AgriLogy/agri-core/commit/53b81c932d3ca0c16336074ffe3b645d44ff91c5))

Closes #15


## v0.6.0 (2026-05-29)

### Features

- **agronomy**: Db-backed compute_et0_for_zone via AgriMainDBClient
  ([`a61c383`](https://github.com/AgriLogy/agri-core/commit/a61c383ee00dac41988f9676c3353ace3cf57f4e))

First fetch-and-compute handler: compute_et0_for_zone(session, zone_id) fetches the previous full
  hour of weather averages for the zone and runs the pure compute_zone_et0 handler. The DB access
  that used to live in the agri-api Django adapter now lives in agri-core.

- AgriMainDBClient.average_value(session, model, zone_id, start, end): AVG(value) over [start, end)
  for a zone; mirrors the Django _avg helper (NULL -> None on empty). Reusable by the remaining
  handler lifts. - compute_et0_for_zone: floor end to the hour, average the 5 weather sensors, pull
  zone.user lat/lon, delegate to compute_zone_et0. Returns None on unknown zone or missing slot
  input. The pure DTO-in handler is untouched so the FAO-56 math stays DB-free and unit-testable. -
  Tests run against in-memory SQLite (portable column types), exercising the real fetch+compute path
  and average_value windowing -- no Postgres.

Depends on agri-db's reverse-relationship fix (mappers must configure before any query).


## v0.5.0 (2026-05-29)

### Chores

- **release**: Pin agri-db by tag + adopt semantic-release; rework CI
  ([`c0be5d9`](https://github.com/AgriLogy/agri-core/commit/c0be5d90e9c8ed426924c9b1967ae54f1d89b33c))

Switch to the full-Revly tag-pinned topology: - agri-db dep: path dep -> 'agri-db @
  git+…/agri-db.git@0.1.0'; drop [tool.uv.sources]; uv.lock now pins the agri-db release commit. -
  Adopt python-semantic-release (bare {version} tag, like agri-db / revly-core): dynamic version
  from src/agri/core/_version.py, release.yml (push-to-main + manual), build_command='' (consumed by
  tag, not wheel), author pinned to mks-zakaria. - CI: replace the agri-db sibling dual-checkout
  with a git-credential step (AGRI_DB_RO_TOKEN) so 'uv sync' fetches the private tagged agri-db. -
  Add conventional-pre-commit hook.

Verified locally: uv sync installs agri-db from the git tag, ruff clean, 87 tests pass.

### Continuous Integration

- Bump uv to 0.11.6 to match the lockfile format
  ([`3754c81`](https://github.com/AgriLogy/agri-core/commit/3754c8140887216682fdbe73bdcb152986764d18))

uv.lock is revision 3 with a dynamic-version editable root; uv 0.5.7 fails to parse it (missing
  field 'version'). Pin CI to the uv that wrote the lock.

- Check out private agri-db sibling for the path dependency
  ([`171e325`](https://github.com/AgriLogy/agri-core/commit/171e3251064afee3689ac5c8f592bc3e058edf9a))

uv sync resolves agri-core's [tool.uv.sources] dep agri-db from ../agri-db, which CI didn't provide.
  Check out both repos side by side under the workspace (agri-core/ + agri-db/) and run uv from
  agri-core/. agri-db is private, so its checkout uses the AGRI_DB_RO_TOKEN secret (fine-grained,
  read-only on agri-db).

### Features

- **database**: Add agri.core.database DB layer over agri-db
  ([`4ac4071`](https://github.com/AgriLogy/agri-core/commit/4ac4071277d00705e16855f4811068228ecbcd79))

Introduce the SQLAlchemy DB-access layer for agri-core, mirroring revly-core/src/revly/database: a
  lazily-created engine (AGRI_DB_URL, NullPool + prepare_threshold=None for the Supabase transaction
  pooler), a session_scope() context manager, and AgriMainDBClient with generic
  get/get_one/list_/exists helpers that take an externally-managed Session.

Wire agri-db as a local editable path dependency via [tool.uv.sources] (no versioning -- solo dev),
  making the api->core->db chain concrete. Handlers still take DTOs; migrating them to fetch via the
  client is the next PR.


## v0.4.0 (2026-05-29)

### Chores

- Apply claude-token-optimizer init ([#9](https://github.com/AgriLogy/agri-core/pull/9),
  [`c8c6e2f`](https://github.com/AgriLogy/agri-core/commit/c8c6e2ffeb878787c5590aa5e03233ef85bc1cab))

- Append Session Start Protocol to CLAUDE.md. - Add .claudeignore (excludes README.md / CHANGELOG.md
  etc. from auto-load). - Add .claude/{COMMON_MISTAKES,QUICK_START,ARCHITECTURE_MAP}.md skeletons +
  docs/INDEX.md.

CLAUDE.md content otherwise preserved.

- Scaffold agri-core shared library
  ([`e5b84dc`](https://github.com/AgriLogy/agri-core/commit/e5b84dc5e2f56e0fcd813eba97b8ffd43af18b4b))

Bootstrap the framework-agnostic shared library for the Agrilogy backend. agri-core holds business
  logic (FAO-56 / Kc / irrigation), device adapters (Bivocom, LoRaWAN/ChirpStack), the alert
  evaluator, and endpoint handlers. No Django, no DRF imports - the DRF wrapper lives in agri-api.
  Imported as `from agri.core import ...`, mirroring the `from agri.db import ...` pattern in
  agri-db.

Includes: - pyproject.toml with src/ layout, setuptools build, ruff + pytest dev deps, pydantic
  runtime dep - src/agri/core/__init__.py with __version__ = "0.0.1" - Makefile with bootstrap /
  sync / lint / format / test targets - CI workflows: ruff + pytest on every push/PR,
  conventional-commit validation on PR titles - README.md and CLAUDE.md anchoring the design rules -
  One smoke test asserting the package imports

### Features

- **agronomy**: Add FAO-56 hourly math + compute_zone_et0 handler
  ([#4](https://github.com/AgriLogy/agri-core/pull/4),
  [`867cb00`](https://github.com/AgriLogy/agri-core/commit/867cb00a91543978331c693bd29e731e8b03a644))

Second handler lift. Bring the FAO-56 hourly Penman-Monteith ET₀ math + the high-level entry point
  out of agri-api into the framework-agnostic shared library.

Adds: - FAO-56 physical constants (ALBEDO, SIGMA hourly variant, SOLAR_CONSTANT_MJ_M2_MIN,
  LST/DEPLOYMENT_LOCAL_TZ, CLOUD_RATIO_MIN/MAX, CLOUD_FACTOR_MIN, CROP_STAGE_PROFILES) - 17 pure
  math helpers: es / ea / Δ / γ / VPD, unit conversions, wind projection, solar geometry (EoT, Ra,
  solar-time correction), radiation balance (cloudiness ratio, Rn, G, ASCE Cn/Cd, is_daytime), and
  the composite penman_monteith_hourly_mm - Et0Inputs DTO (zone identity + sensor-unit weather
  averages + lat/lon/elevation/wind_height) and ZoneEt0 dataclass with as_dict -
  compute_zone_et0(inputs) handler: returns None on missing input, computes Ra if lat/lon supplied,
  falls back to 0.75 heuristic - 30 unit tests covering constants, each helper, the Penman-Monteith
  composite, and the handler's None / lat-lon / timestamp paths

Bumps version to 0.2.0.

- **agronomy**: Add framework-agnostic field_snapshot handler
  ([#2](https://github.com/AgriLogy/agri-core/pull/2),
  [`88e761f`](https://github.com/AgriLogy/agri-core/commit/88e761f85e83d3f674b0c9085e7ee022784b7581))

First real handler lift. Move the FAO-56 / doc § 3-4 irrigation decision logic out of
  agri-api/back/agriBack/agronomy.py and into agri.core.agronomy, with new DTOs (ZoneParams,
  SensorAggregates, FieldInputs) so the ORM never crosses the boundary.

Adds: - Constants (DEFAULT_KC, RAIN_FORECAST_TRIGGER_MM, ...) - Pure math (effective_rainfall_mm,
  etc_mm, update_daily_depletion, cumulative_dr_after_missed_days) - Decision (IrrigationDecision
  dataclass, irrigation_decision_dr, _format_decision) - Handler (field_snapshot) - 28 unit tests
  covering each branch + the email key contract

Bumps version to 0.1.0.

- **alerts**: Add framework-agnostic alert evaluator + sensor-key registry
  ([#6](https://github.com/AgriLogy/agri-core/pull/6),
  [`61c54ce`](https://github.com/AgriLogy/agri-core/commit/61c54ceb02f817accd8f6676bc5f656a8ef1b3f2))

Third handler lift per project_agri_core_architecture. Move the framework-agnostic pieces of the
  alert engine out of agri-api into the shared library.

Adds agri.core.alerts: - SENSOR_KEY_REGISTRY: 35-entry metadata table (unit / French label / type /
  model name string). agri-core treats the model name as opaque metadata; the agri-api adapter
  resolves it to a Django class. - Condition constants (GREATER_THAN, LESS_THAN, EQUAL_TO,
  EQUALITY_TOLERANCE). - evaluate(condition, threshold, value): pure predicate; None never fires;
  unknown condition raises ValueError. - AlertSpec DTO + evaluate_alert(spec, value): bind the
  predicate without leaking ORM rows. - LatestReading dataclass. -
  suggested_alert_payload(sensor_key, recent_values): pure payload assembly for the create-alert
  prefill feature. - 15 unit tests.

Bumps version to 0.3.0.

- **notifications**: Add compose_notification_email
  ([#8](https://github.com/AgriLogy/agri-core/pull/8),
  [`5be37dd`](https://github.com/AgriLogy/agri-core/commit/5be37dd301daec2bb47d11f6868816e9ff89d3a5))

Fourth handler lift per project_agri_core_architecture. Move the French notification-email
  composition into agri-core.

Adds agri.core.notifications: - compose_notification_email(user_name, snapshot) -> str: pure
  renderer that turns a field_snapshot dict into the email body. - _fmt helper for missing-value
  placeholders. - 4 unit tests rendering against fabricated snapshots.

Bumps version to 0.4.0.
