# CHANGELOG


## v0.21.0 (2026-07-12)

### Features

- **db**: Resolve reading ownership via the device JOIN (device-keyed)
  ([#58](https://github.com/AgriLogy/agri-core/pull/58),
  [`9053f7c`](https://github.com/AgriLogy/agri-core/commit/9053f7c1cb360ff228c9476f02adfd1a6132eb2f))

Phase 3: resolve a reading's owner via LEFT JOIN to analytics_device — COALESCE(device.owner,
  row.owner). Device-sourced rows follow their device on transfer (one-row analytics_device update
  moves the whole history, no reading rewrite); non-device rows fall back to their own user/zone.
  Covers hourly_averages / average_value / sum_value / latest_reading + the alert reads. Tests
  create analytics_device; 125 pass.

Closes #57


## v0.20.0 (2026-07-12)

### Chores

- **deps**: Bump agri-db pin 0.14.0 -> 0.15.0 (device_id on readings)
  ([#55](https://github.com/AgriLogy/agri-core/pull/55),
  [`75248f6`](https://github.com/AgriLogy/agri-core/commit/75248f6213a5b2ddd4fc83c9fb87621428df7352))

Picks up the additive device_id column on the 37 sensor-reading tables (agri-db #59). No code
  change; models load + 125 tests pass.

Closes #54

### Features

- **deps**: Release agri-db 0.15.0 pin (device_id on reading tables)
  ([#56](https://github.com/AgriLogy/agri-core/pull/56),
  [`d01026f`](https://github.com/AgriLogy/agri-core/commit/d01026fdfd5ba98b18c7a9d031ca9c4fd484b9e1))

The 0.15.0 bump merged as chore(deps), which semantic-release does not release. Cut a proper
  agri-core release so downstream agri-api can pin it and pick up the device_id column (phase 0 of
  device-keyed ownership).


## v0.19.0 (2026-07-03)

### Features

- **deps**: Bump agri-db pin 0.12.0 -> 0.14.0 ([#53](https://github.com/AgriLogy/agri-core/pull/53),
  [`623b00a`](https://github.com/AgriLogy/agri-core/commit/623b00ac5bfe27ac77d94973a6c6ca136503f69b))

Picks up the 15 ensure-script tables now in Alembic + FeedbackBugreport + CustomUser.is_technician.
  Test fixtures set is_technician=False (NOT NULL, no server default — Django supplies it in prod)
  to match the new column. Unblocks the fastapp /feedback port (needs FeedbackBugreport) and closes
  the api->core->db pin gap.


## v0.18.2 (2026-06-28)

### Bug Fixes

- **deps**: Bump agri-db pin 0.11.1 -> 0.12.0 (analytics_devicesensor) (#48)
  ([#49](https://github.com/AgriLogy/agri-core/pull/49),
  [`d6fb112`](https://github.com/AgriLogy/agri-core/commit/d6fb1128da3821cd8de87c63f1010cfb8b2aff93))

Pulls agri-db 0.12.0 so downstream agri-api bundles the analytics_devicesensor migration for the
  admin device->sensor mapping feature. fix-typed so semantic-release cuts the version. Tests green.

Closes #48.


## v0.18.1 (2026-06-28)

### Bug Fixes

- **deps**: Release the agri-db 0.11.1 pin (#45)
  ([#47](https://github.com/AgriLogy/agri-core/pull/47),
  [`5fc59f0`](https://github.com/AgriLogy/agri-core/commit/5fc59f0f84159c23fec9a5c432e95c5a328e2827))

The pin bump merged as chore(deps) so semantic-release cut no version, leaving the 0.11.1 bump
  unreleased and unavailable to downstream agri-api. This patch-typed commit cuts the release; also
  documents the >= live-schema-head floor on the pin.

### Chores

- **deps**: Bump agri-db pin 0.8.0 -> 0.11.1 (#45)
  ([#46](https://github.com/AgriLogy/agri-core/pull/46),
  [`808bba6`](https://github.com/AgriLogy/agri-core/commit/808bba6a7ffd71b89f88f9f6c40a1ca4798ff814))

Three minor versions behind the live schema. Bump so downstream agri-api can apply the current
  migration chain and run on the current schema. agri-core test suite green (125 passed) against
  0.11.1.

Closes #45.


## v0.18.0 (2026-06-26)

### Features

- **agronomy**: Multi-day ET0 forecast compute
  ([#44](https://github.com/AgriLogy/agri-core/pull/44),
  [`cce97b1`](https://github.com/AgriLogy/agri-core/commit/cce97b13d7823084adf0c981e42fe7b3daecea37))

Pure, deterministic 7-day reference-ET0 forecast: maps N days of daily weather aggregates to one ET0
  (mm/day) per day by summing the existing FAO-56 hourly Penman-Monteith handler over a synthesised
  diurnal day. No I/O / no data source here — the weather provider lives in the agri-api adapter
  (mock-first). For agrilogy-front #18.


## v0.17.0 (2026-06-25)

### Features

- **alerts**: Percentile + sd strategies for suggested_alert_payload
  ([#42](https://github.com/AgriLogy/agri-core/pull/42),
  [`d07577c`](https://github.com/AgriLogy/agri-core/commit/d07577cb5d245f18b962bb1e1b28224c070dbd72))

AlertSuggest could only prefill the threshold from the mean of recent readings. Add a strategy arg
  (default 'mean', back-compatible) with direction-aware 'percentile' (p90 for GREATER_THAN / p10
  for LESS_THAN) and 'sd' (mean ± 2σ) options, threaded through suggest_alert_for. Pure +
  deterministic (stdlib statistics/math). 11 new unit tests.


## v0.16.0 (2026-06-25)

### Features

- **notifications**: Render the daily email in the user's language (fr/ar)
  ([#40](https://github.com/AgriLogy/agri-core/pull/40),
  [`d59f5f4`](https://github.com/AgriLogy/agri-core/commit/d59f5f48523dbbd3efe529da2653f17b3b605f04))

compose_notification_email gains a language arg (default 'fr'); adds an Arabic template mirroring
  the French one (same placeholders/values). compose_notification_for_user reads
  user.preferred_language (agri-db 0.8.0) and passes it through. Bumps agri-db pin 0.7.0->0.8.0. 4
  new tests.

Arabic copy needs native-speaker review.


## v0.15.0 (2026-06-25)

### Features

- **alerts**: Resolve notification-zone alert streams via sensor assignment
  ([#38](https://github.com/AgriLogy/agri-core/pull/38),
  [`7aec53b`](https://github.com/AgriLogy/agri-core/commit/7aec53b019e22dc1f6a237db70f31698f028a126))

For agrilogy-front #57 custom notification zones. Add effective_zone_id_for_alert(): a farm-zone
  alert keeps its zone_id; a notification-zone alert resolves its reading stream through the
  matching AnalyticsNotificationzonesensor (sensor_key -> source_zone_id); neither = user-wide.
  recent_triggers_for_user now scopes the latest reading by the effective zone. Bumps agri-db pin
  0.6.0->0.7.0 (notification-zone schema).


## v0.14.0 (2026-06-25)

### Chores

- **ci**: Auto-assign new issues and PRs to mks-zakaria
  ([#32](https://github.com/AgriLogy/agri-core/pull/32),
  [`db211e8`](https://github.com/AgriLogy/agri-core/commit/db211e875a49ac018292e398883ec066b748115e))

### Continuous Integration

- Fix Auto Assign workflow failing on pull_request events
  ([#34](https://github.com/AgriLogy/agri-core/pull/34),
  [`1af5a99`](https://github.com/AgriLogy/agri-core/commit/1af5a99cedeca5a73bcc763308a9a1df6bc2cca9))

Replace pozil/auto-assign-issue@v1 (which errors with "Couldn't find issue info in current context"
  on pull_request, and warns on the invalid numOfAssignee input) with a single gh-api call to the
  issues/assignees endpoint, which assigns both issues and PRs since a PR shares its repo's
  issue-number space.

### Features

- **agronomy**: Use zone.elevation_m in compute_et0_for_zone
  ([#36](https://github.com/AgriLogy/agri-core/pull/36),
  [`2d683e1`](https://github.com/AgriLogy/agri-core/commit/2d683e1594cdd1f4f8791fc9fd11ad5a248d95c1))

Pass elevation_m=zone.elevation_m into Et0Inputs so the clear-sky radiation Rso = (0.75 +
  2e-5*elevation_m)*Ra is correct away from sea level instead of assuming 0 m. Requires the
  elevation_m column added in agri-db 0.6.0; pin bumped 0.2.0 -> 0.6.0.

Supports agri-api #15.


## v0.13.0 (2026-06-12)

### Features

- **alerts**: Register vpd sensor key (VPDWeather)
  ([#30](https://github.com/AgriLogy/agri-core/pull/30),
  [`3cdc65a`](https://github.com/AgriLogy/agri-core/commit/3cdc65a488100297a4187899784fa71a10044c21))

Lets the frontend DPV card create/suggest alerts; VPDWeather rows are written by the ET0 calc task.


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
