# Shared BLE Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Wahoo Server runs FTMS + CPS in one worker process sharing one BLE connection (Windows forbids two processes on one KICKR).

**Architecture:** see the spec. SharedBackend (handles over one real bleak client) → server worker (one asyncio loop, stdin control) → WorkerProcess + reworked ServerSupervisor.

**Tech Stack:** Python 3.10+, asyncio, bleak, pytest. Env: `source "$(conda info --base)/etc/profile.d/conda.sh" && conda activate wahoo`.

**Spec:** `docs/superpowers/specs/2026-11-06-shared-ble-worker-design.md` (binding; read it fully).

## Global Constraints

- Never modify files outside `src/wahoo_lsl/apps/`, `packaging/`, `docs/windows-apps.md`, `docs/superpowers/`, `tests/test_apps_*.py` and new test helper files. `pyproject.toml` may change. Existing code under `src/wahoo_lsl/` (ble, cli, config, ftms/*, dashboard/*) is read-only.
- Never touch the untracked `config.json`, `config.json~`, `ftms.json`, `logs/`.
- Never run `wahoo-server`, `wahoo-dashboard`, `wahoo-dashboard-app`, `wahoo-ftms serve`, `wahoo-lsl run/scan/list-features`, or `wahoo-worker` against real BLE; tests use fakes only.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (this overrides any other trailer reminder).
- Minimal, clean Python; follow the style of the existing `src/wahoo_lsl/apps/` modules.
- Full suite must pass: `python -m pytest -q`.

## Review Focus

- A drop while both handles are attached: each service's callback fires exactly once and both reconnect onto one real client.
- FTMS off / session stop must reach `controller.close` (0 W spin-down) before the handle detaches; CPS keeps streaming across FTMS off.
- Stale callbacks or GATT calls from an old epoch must never affect the current link.
- Server dies (stdin EOF) → worker stops FTMS then CPS and exits.
- Exactly one final `("stopped", reason)` from the supervisor on every path.

---

### Task 1: SharedBackend

**Files:** Create `src/wahoo_lsl/apps/shared_ble.py`, `tests/test_apps_shared_ble.py`.

**Interfaces:** Produces `SharedBackend(inner: BleBackend | None = None)` implementing `discover`, `find_by_address`, `make_client(device, disconnected_callback) -> Handle`; `Handle` with `connect`, `disconnect`, `is_connected`, `services`, `read_gatt_char`, `write_gatt_char`, `start_notify`, `stop_notify`.

- [ ] Read spec §1 and how `CpsClient` (`src/wahoo_lsl/ble.py`) and `FtmsLink` (`src/wahoo_lsl/ftms/link.py`) use their client and backend; read `tests/fakes.py` / `tests/ftms_fakes.py` fakes.
- [ ] Write failing tests for every bullet under spec Testing → `test_apps_shared_ble.py`.
- [ ] Implement; tests pass; full suite passes.
- [ ] Commit `feat(apps): SharedBackend — one BLE link shared by several clients`.

### Task 2: Server worker

**Files:** Create `src/wahoo_lsl/apps/server_worker.py`, `tests/test_apps_server_worker.py`; modify `src/wahoo_lsl/apps/worker.py` (add `server` mode, update USAGE/docstring).

**Interfaces:** Consumes `SharedBackend`. Produces CLI `wahoo-worker server -c <cps cfg> -f <ftms cfg> [--no-ftms] [--debug]`; stdin commands `ftms on|ftms off|stop`; report lines `[worker] ftms exited with code N`, `[worker] cps exited with code N`; label format per spec §2. Make `main(argv, *, backend=None, stdin=None)` (or equivalent injection) so tests drive it in-process with fakes.

- [ ] Read spec §2; read `cli.run_session`, `ftms.cli._serve`, `cli.setup_logging`.
- [ ] Write failing tests for every bullet under spec Testing → `test_apps_server_worker.py`.
- [ ] Implement; tests pass; full suite passes.
- [ ] Commit `feat(apps): wahoo-worker server — FTMS and CPS in one process`.

### Task 3: WorkerProcess, supervisor, paths, docs

**Files:** Create `src/wahoo_lsl/apps/worker_process.py`, `tests/server_fake_worker.py`; modify `src/wahoo_lsl/apps/supervisor.py`, `src/wahoo_lsl/apps/paths.py`, `src/wahoo_lsl/apps/server_app.py` (supervisor construction only), `tests/test_apps_supervisor.py`, `tests/test_apps_paths.py`, `tests/test_apps_server.py` (only where it asserts old commands), `docs/windows-apps.md`, and the amended rows of `docs/superpowers/specs/2026-10-27-windows-apps-design.md` if they now contradict.

**Interfaces:** Consumes the worker CLI/protocol from Task 2. `ServerSupervisor` public interface and posted events unchanged (spec §4); `service_commands(debug=False, ftms_enabled=True) -> {"worker": argv}`.

- [ ] Read spec §3–§4 and Docs; read current `supervisor.py`, `server_app.py`, `dashboard/launcher.py` (`Child`, `_clean_code`).
- [ ] Write the fake worker and failing supervisor/paths tests per spec Testing.
- [ ] Implement; remove the TCP port probe and `confirm_zero` use; tests pass; full suite passes.
- [ ] Update `docs/windows-apps.md` (one worker, in-process spin-down, launcher limitation on Windows, new checklist steps).
- [ ] Commit `feat(apps): Wahoo Server drives one shared-BLE worker`.
