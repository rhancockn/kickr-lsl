# FTMS Allowed Hosts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The FTMS service optionally accepts plain-TCP clients from listed LAN machines (DNS names or IPs), checked per connection.

**Architecture:** A pure host check (`ftms/allow.py`) with an injected resolver. New config keys `allowed_hosts` and `listen_host`. `ControlServer` runs the check before the busy check; `cli._serve` binds `listen_host` in remote mode and warns about names that don't resolve.

**Tech Stack:** Python ≥ 3.10, asyncio, standard library, pytest. Env: `source "$(conda info --base)/etc/profile.d/conda.sh" && conda activate wahoo`.

**Spec:** `docs/superpowers/specs/2026-11-06-ftms-allowed-hosts-design.md` (binding; read it fully).

## Global Constraints

- New code uses the standard library only.
- Existing tests keep passing. The only exception is a test whose assertion the spec changes on purpose; none is expected.
- `FtmsSettings` keeps its current positional field order. New fields are appended with defaults, because `tests/test_ftms_config.py` builds `FtmsSettings("localhost", 50000, 4.0, 0.5, 600)`.
- `ControlServer(controller, host, port)` keeps working. New parameters are keyword-only with defaults.
- The start-up log prefix `Accepting client commands on` stays exact, because the Windows app matches it.
- A refused peer never touches `_active`, never calls `spin_down` and never calls `reset_slew`.
- Never touch the untracked `config.json`, `config.json~`, `ftms.json` or `logs/`. Never run `wahoo-ftms serve` or other commands against real BLE.
- Commit trailer, exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Full suite passes: `python -m pytest -q`.

## Review Focus

1. **A hanging resolver.** One name's lookup never returns. The check still answers within the bound, other entries still match, and the handler doesn't hang. Test: `test_slow_name_bounded_other_matches` (Task 1).
2. **A refused client while an allowed session runs.** The session is unaffected: no spin-down, and its next `read` works. Test: `test_refused_peer_does_not_disturb_active_session` (Task 3).
3. **An IPv4-mapped peer address.** A peer seen as `::ffff:10.0.0.23` matches entry `10.0.0.23` and a name resolving to it. Test: `test_ipv4_mapped_peer` (Task 1).
4. **A remote-mode config with `host: "localhost"`.** It is rejected with a message naming `ftms.host`. Test: `test_allowed_hosts_requires_ipv4_loopback_host` (Task 2).
5. **The start-up line in remote mode.** It keeps the exact prefix, so the Windows app still sees FTMS as ready. Test: `test_serve_remote_mode_binds_listen_host_and_logs_prefix` (Task 3).

---

### Task 1: Host check

**Files:**
- Create: `src/wahoo_lsl/ftms/allow.py`
- Test: `tests/test_ftms_allow.py`

**Interfaces:**
- Produces:
  - `Resolver = Callable[[str], Awaitable[list[str]]]`.
  - `def normalize_ip(text: str) -> str | None`: returns the canonical IP string or `None` if `text` is not an IP. It maps IPv4-mapped IPv6 to IPv4 and drops any `%scope`.
  - `async def resolve_host(name: str) -> list[str]`: production resolver. It uses `asyncio.get_running_loop().getaddrinfo(name, None)`, returns normalised addresses with duplicates removed, and returns `[]` on `OSError`.
  - `async def is_allowed(peer_ip: str, allowed: Sequence[str], resolve: Resolver = resolve_host, timeout_s: float = 2.0) -> bool`.
  - `CHECK_TIMEOUT_S = 2.0`.

- [ ] **Step 1: Write failing tests** in `tests/test_ftms_allow.py`, using a fake resolver dict `{"lab-pc-3.lab.local": ["10.0.0.23"], "other": ["10.0.0.99"]}` and `asyncio.run`:
  - `test_loopback_always_allowed`: `127.0.0.1`, `127.5.5.5` and `::1` are allowed with `allowed=[]` and with `["other"]`.
  - `test_ip_literal_match_and_mismatch`: `10.0.0.23` against `["10.0.0.23"]` is True; against `["10.0.0.24"]` is False.
  - `test_name_resolves_to_peer`: `10.0.0.23` against `["lab-pc-3.lab.local"]` is True.
  - `test_name_resolves_elsewhere`: `10.0.0.23` against `["other"]` is False.
  - `test_names_case_insensitive`: the entry `LAB-PC-3.LAB.LOCAL` matches. The fake resolver lowercases its key.
  - `test_failing_name_does_not_block_other`: the resolver raises `OSError` for `bad` and `["bad", "lab-pc-3.lab.local"]` is True; `["bad"]` is False.
  - `test_slow_name_bounded_other_matches`: the resolver sleeps 10 s for `slow`, with `timeout_s=0.1`. `["slow", "10.0.0.23"]` is True. `["slow", "lab-pc-3.lab.local"]` returns True within 0.5 s if resolution runs in parallel. `["slow"]` returns False within 0.5 s.
  - `test_ipv4_mapped_peer`: `::ffff:10.0.0.23` matches `["10.0.0.23"]` and `["lab-pc-3.lab.local"]`.
  - `test_normalize_ip`: `"::ffff:10.0.0.23"` gives `"10.0.0.23"`, `"fe80::1%eth0"` gives `"fe80::1"`, and `"lab-pc"` gives `None`.
- [ ] **Step 2: Run them and confirm they fail.** Run `python -m pytest tests/test_ftms_allow.py -q`. Expected: ImportError / FAIL.
- [ ] **Step 3: Implement `allow.py`.**
  - IP entries are compared after `normalize_ip`.
  - Name entries are lowercased and resolved concurrently with `asyncio.wait(..., timeout=timeout_s)`.
  - Pending lookups are cancelled at the timeout. Exceptions from finished lookups are logged at DEBUG.
  - The function returns True as soon as any entry matches.
- [ ] **Step 4: Run them and confirm they pass.** Run `python -m pytest tests/test_ftms_allow.py -q`. Expected: all pass.
- [ ] **Step 5: Commit** with message `feat(ftms): allowed-host check by DNS name or IP`.

### Task 2: Config keys

**Files:**
- Modify: `src/wahoo_lsl/ftms/config.py` (`_FTMS_KEYS`, `FtmsSettings`, `_parse_ftms`), `ftms.example.json`
- Test: `tests/test_ftms_config.py` (add tests only)

**Interfaces:**
- Produces: `FtmsSettings` gains two appended fields:
  - `allowed_hosts: tuple[str, ...] = ()`, lowercased and stripped;
  - `listen_host: str = "0.0.0.0"`.

  It also gains a property `remote: bool` (`bool(self.allowed_hosts)`) and a property `bind_host: str` (`listen_host if remote else host`).

- [ ] **Step 1: Write failing tests** in `tests/test_ftms_config.py`, using the file's existing `cfg(**ftms)` helper:
  - `test_allowed_hosts_default_local_only`: `cfg().ftms.allowed_hosts == ()`, `remote` is False, and `bind_host == "127.0.0.1"`.
  - `test_allowed_hosts_parsed`: `cfg(allowed_hosts=["Lab-PC-3.lab.local", "10.0.0.23"])` gives `("lab-pc-3.lab.local", "10.0.0.23")`, `remote` is True, and `bind_host == "0.0.0.0"`.
  - `test_allowed_hosts_rejects_bad` (parametrized over `"lab-pc"`, `[""]`, `["a b"]`, `[1]` and `None`): raises `ConfigError` matching `ftms.allowed_hosts`.
  - `test_listen_host_custom`: `cfg(allowed_hosts=["x"], listen_host="192.168.1.5").ftms.bind_host == "192.168.1.5"`.
  - `test_listen_host_rejects_bad` (parametrized over `"lab-pc"`, `"::"`, `"fe80::1"` and `5`), each with `allowed_hosts=["x"]`: raises matching `ftms.listen_host`.
  - `test_listen_host_needs_allowed_hosts`: `cfg(listen_host="0.0.0.0")` raises matching `listen_host needs allowed_hosts`.
  - `test_allowed_hosts_requires_ipv4_loopback_host` (parametrized over `localhost` and `::1`), each with `allowed_hosts=["x"]`: raises matching `with allowed_hosts, ftms.host must be 127.0.0.1`.
  - `test_example_config_parses`: `load_ftms_config("ftms.example.json")` loads with `allowed_hosts == ()`, if no such test exists already.
- [ ] **Step 2: Run them and confirm they fail.** Run `python -m pytest tests/test_ftms_config.py -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement the validation** exactly as the spec's Config section and these messages say. Then add `"allowed_hosts": []` to `ftms.example.json`, after `"port"`.
- [ ] **Step 4: Run them and confirm they pass.** Run `python -m pytest tests/test_ftms_config.py tests/test_apps_paths.py tests/test_apps_server.py -q`. Expected: all pass.
- [ ] **Step 5: Commit** with message `feat(ftms): allowed_hosts and listen_host config`.

### Task 3: Server, CLI, docs

**Files:**
- Modify: `src/wahoo_lsl/ftms/server.py`, `src/wahoo_lsl/ftms/cli.py` (`_serve`), `README.md` (FTMS section)
- Test: `tests/test_ftms_server.py` and `tests/test_ftms_cli.py` (add tests)

**Interfaces:**
- Consumes: `is_allowed`, `resolve_host` and `Resolver` (Task 1); `FtmsSettings.allowed_hosts`, `.remote` and `.bind_host` (Task 2).
- Produces:
  - `ControlServer(controller, host, port, *, allowed: Sequence[str] = (), resolve: Resolver = resolve_host)`.
  - A module-level `def peer_ip(writer) -> str` in `server.py`, returning `writer.get_extra_info("peername")[0]`. Tests monkeypatch `wahoo_lsl.ftms.server.peer_ip` to fake a LAN peer.
  - The error code `not_allowed`.

- [ ] **Step 1: Write failing server tests** in `tests/test_ftms_server.py`, reusing `StubController`, `until` and `serving`, extended with the `allowed` and `resolve` keywords:
  - `test_refused_peer_gets_not_allowed_and_closed`:
    - Setup: `allowed=["lab-pc-3"]`, the resolver maps `lab-pc-3` to `["10.0.0.23"]`, and `peer_ip` is patched to return `"10.0.0.99"`.
    - The client reads one line: `{"ok": false, "error": "not_allowed", ...}`, whose `detail` contains `10.0.0.99` and `ftms.allowed_hosts`. Then it reads EOF.
    - `ctl.calls == []`, and the log contains `Refusing 10.0.0.99: not in allowed_hosts`.
  - `test_allowed_peer_served`: `peer_ip` returns `"10.0.0.23"`, and a `read` round trip works.
  - `test_refused_peer_does_not_disturb_active_session`:
    - An allowed client is connected first.
    - `peer_ip` is patched with a side-effect function so that the next connection reports `10.0.0.99`.
    - The second connection gets `not_allowed`. The first client's next `read` still works, and `ctl.spin_downs == []`.
  - `test_busy_between_allowed_peers`: with two allowed peers, the second gets `busy`.
  - `test_empty_allowed_skips_check`: with the default `allowed`, a resolver that raises if called is never used.
- [ ] **Step 2: Write failing CLI tests** in `tests/test_ftms_cli.py`, following the existing `_serve` tests' fake backend, their port handling and `caplog`:
  - `test_serve_remote_mode_binds_listen_host_and_logs_prefix`:
    - Config `allowed_hosts=["127.0.0.1"]` and `listen_host="127.0.0.1"`. Use 127.0.0.1 so the test needs no LAN.
    - The log contains a line starting `Accepting client commands on 127.0.0.1:` and containing `(local + allowed: 127.0.0.1)`.
  - `test_serve_warns_unresolvable_name`:
    - Config `allowed_hosts=["no-such-host.invalid"]`, `listen_host="127.0.0.1"`.
    - Monkeypatch `wahoo_lsl.ftms.allow.resolve_host` (or the name `cli` imports) to return `[]`.
    - A WARNING contains `allowed host 'no-such-host.invalid' does not resolve`, and the service still starts.
- [ ] **Step 3: Run them and confirm they fail.** Run `python -m pytest tests/test_ftms_server.py tests/test_ftms_cli.py -q`. Expected: the new tests fail.
- [ ] **Step 4: Implement.**
  - In `_handle`, the allow check runs first: when `allowed` is non-empty, run `is_allowed(peer_ip(writer), ...)`. If refused, send the error, close, `wait_closed` with `ConnectionError` suppressed, and return. Only then the busy check.
  - `start_serving` logs `Accepting client commands on %s:%d` plus ` (local + allowed: a, b)` when `allowed` is non-empty.
  - `_serve` passes `cfg.ftms.bind_host`, `allowed=cfg.ftms.allowed_hosts`. In remote mode it resolves each non-IP entry once before `bind`, and logs the warning for any empty result.
  - `Cannot listen on` reports `bind_host`.
  - Update the docstring at the top of `server.py`.
- [ ] **Step 5: Run the tests and confirm they pass.** Run `python -m pytest -q`. Expected: the full suite passes.
- [ ] **Step 6: Update the README FTMS section.**
  - Add a "Remote clients (allowed hosts)" subsection: the keys, an example `ftms.json` fragment, and the accepted risk in one sentence. Note that local tools must dial `127.0.0.1`, because the PC's own LAN address is refused unless listed. Add the firewall note.
  - Add a short wire-protocol reference for plain-socket clients covering the connection rules, the requests and replies, the status fields and the error codes. Note that `busy` and `not_allowed` arrive immediately after connecting.
  - Add `not_allowed` to the error list.
- [ ] **Step 7: Commit** with message `feat(ftms): accept clients from allowed hosts; protocol reference`.
