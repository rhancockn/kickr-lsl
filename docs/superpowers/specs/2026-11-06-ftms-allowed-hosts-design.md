# FTMS service: remote clients from allowed hosts

Branch `feat/ftms-allowed-hosts`, from `main`.

## Why

Lab PCs on the LAN need to control the KICKR through the FTMS service. They might run
experiment code in Python, MATLAB or Unity. Today the service listens on localhost only.

The mutual-TLS branch (`feat/ftms-remote-tls`) solves remote access securely, but every client
language then needs TLS with pinned certificates, which is hard in MATLAB and Unity. This
change replaces TLS **for now** with a plain-TCP listener that accepts only listed machines.
Any language can then use a plain socket and newline-delimited JSON. The TLS branch stays
parked.

## Goals

- Optionally accept FTMS clients from specified LAN machines, listed by DNS name (preferred)
  or IP address.
- Connections from the server PC itself keep working unchanged: Wahoo Server, Wahoo Dashboard,
  `wahoo-dashboard`, `wahoo-ftms read/set` and `KickrClient()`.
- Without the new setting, behaviour is exactly as today: localhost only.
- No client changes. The wire protocol gains one error code.

## Non-goals

- Encryption or authentication beyond the source address.
- Changes to the Windows apps, the installer or the dashboard launcher.
- More than one simultaneous client.
- Reconciling with the TLS branch. That happens if and when TLS returns.

## Anti-goals

- **A refused machine must never affect the session.** It never takes the client slot,
  never triggers a spin-down, and never changes the controller's state.
- **No accidental exposure.** The service listens on the network only when `allowed_hosts`
  is non-empty.
- **No breakage of the Windows server app.** It relies on the log line prefix
  `Accepting client commands on` (`src/wahoo_lsl/apps/supervisor.py`, `server_worker.py`).
  That prefix must stay exact.

## Accepted risk

This was decided with the user. Traffic is unencrypted. Any program on an allowed PC can take
control, and so can anyone on the LAN who can impersonate an allowed PC's address. This is
acceptable for this lab.

## Constraints

- This change deliberately modifies existing, tested FTMS code (`ftms/config.py`,
  `ftms/server.py`, `ftms/cli.py`). The existing tests must keep passing, apart from tests
  whose assertions this design changes on purpose.
- The single client slot and the spin-down to 0 W on client disconnect are unchanged.
- Do not touch the untracked `config.json`, `config.json~`, `ftms.json` or `logs/`.
- Python ≥ 3.10, standard library only for the new code.

## Design (decided)

### Config (`ftms` section of `ftms.json`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `host` | loopback host | `127.0.0.1` | Unchanged and still loopback-only. The address local tools dial. |
| `allowed_hosts` | list of strings | `[]` | DNS names or IP literals of machines allowed to connect. Empty means local-only (today's behaviour). |
| `listen_host` | IP literal | `"0.0.0.0"` | Bind address, used only when `allowed_hosts` is non-empty. It can be set to one network interface's address. |

Validation raises `ConfigError` naming the key:
- `allowed_hosts` must be a list of non-empty strings without whitespace.
- `listen_host` must be a valid IP literal.
- `listen_host` given without `allowed_hosts` is an error ("listen_host needs allowed_hosts"),
  so the config never implies network access that isn't there.
- Entries are compared case-insensitively. Duplicates are harmless.
- *(Builder's call, settled.)* Remote mode is **IPv4 only**:
  - `listen_host` must be an IPv4 literal.
  - With `allowed_hosts` set, `host` must be `127.0.0.1`. A `::1` or `localhost` `host` would
    not reach an IPv4 wildcard listener, so it is a `ConfigError` ("with allowed_hosts, ftms.host
    must be 127.0.0.1").
  - IPv6 handling in `is_allowed` (mapped addresses, scope ids) stays for robustness.

`ftms.example.json` gains `"allowed_hosts": []`.

**Why a separate `listen_host`:** local tools dial `ftms.host`. A server bound to `0.0.0.0`
still answers on `127.0.0.1`. Binding both `127.0.0.1:port` and `0.0.0.0:port` at once fails
on Windows and Linux. Keeping `host` loopback therefore leaves every local dialler untouched.

### Host check (new module `src/wahoo_lsl/ftms/allow.py`)

`async def is_allowed(peer_ip: str, allowed: Sequence[str], resolve: Resolver) -> bool`
- **Normalisation:** the peer address is normalised first. An IPv4-mapped IPv6 address
  (`::ffff:10.0.0.23`) becomes `10.0.0.23`, and any IPv6 scope id (`%eth0`) is dropped.
- **Loopback** peers (`127.0.0.0/8`, `::1`) are always allowed.
- *(Builder's call, settled.)* A process on the server PC that dials the PC's own LAN address
  arrives with a non-loopback address. It is refused unless that address or name is listed.
  The README says so: local tools should dial `127.0.0.1`.
- **IP-literal entries** match by address equality.
- **Name entries** are resolved by `resolve(name) -> list[str]`, all in parallel. They match
  if any resolved address equals the peer.
  - The production resolver is `loop.getaddrinfo(name, None)`, taking A and AAAA records with
    the same normalisation.
  - The whole check is bounded at **2 s** in total.
  - A name that fails or times out simply doesn't match, logged at DEBUG; other entries still
    count.
- Names are resolved **on every connection**, with no cache, so a DHCP address change is
  picked up immediately. Connections are rare, so this is cheap.
- The resolver is injected so tests need no network.

### Server (`src/wahoo_lsl/ftms/server.py`)

- `ControlServer(controller, host, port, allowed=(), resolve=None)`. With a non-empty
  `allowed` it binds `host` = `listen_host`; `cli._serve` decides which address to pass.
- In `_handle`, **first**, before the busy check:
  - Get the peer IP and run `is_allowed`.
  - **If refused:** send `{"ok":false,"error":"not_allowed","detail":"this machine (<ip>) is not
    in ftms.allowed_hosts"}`, close, and log WARNING `Refusing <ip>: not in allowed_hosts`. Then
    return. It does not touch `_active`, does not spin down and does not reset slew.
  - With an empty `allowed` list, skip the check (only loopback can reach the service anyway).
- **Start-up log line:** stays `Accepting client commands on <host>:<port>` with the exact
  prefix. In remote mode, append ` (local + allowed: a, b, …)`.
- **Unchanged:** the busy check, dispatch, spin-down on disconnect and close.

### CLI (`src/wahoo_lsl/ftms/cli.py`, `_serve`)

- Bind `listen_host` when `allowed_hosts` is non-empty, else `host`.
- **At start-up,** resolve each name once and log a WARNING for any that doesn't resolve now
  (`allowed host 'lab-pc-9' does not resolve`). This is only a hint for typos; it never
  refuses to start.
- **"Cannot listen on …"** keeps its current behaviour and exit code 2.

### Client side

- Existing clients, including `KickrClient`, already surface error replies, so `not_allowed`
  reaches them the same way `busy` does: as the reply to their first request, raised in Python
  as `KickrError("not_allowed", …)`.
- Remote PCs connect with `KickrClient("server-pc.lab.local", 47610)` or a plain socket.

### Docs

- **README, FTMS section:**
  - the new keys, with a remote-access example;
  - the accepted risk, in one sentence;
  - the firewall note: Windows installs already open the port on the private profile, while
    macOS and Linux may prompt or need a rule;
  - the error list gains `not_allowed`.
- **Protocol reference.** A short client API section for MATLAB, Unity and other plain-socket
  clients. It can live in the README FTMS section or in `docs/ftms-protocol.md` (the builder
  picks). Contents:
  - the connection rules;
  - the `set`, `slew` and `read` requests and their replies;
  - the status fields;
  - the error codes;
  - the fact that `busy` and `not_allowed` arrive immediately after connecting.

## Tests (required)

- **Config:**
  - defaults;
  - a valid list;
  - non-list or empty-string entries are rejected;
  - an invalid or IPv6 `listen_host` is rejected;
  - `host` other than `127.0.0.1` with `allowed_hosts` is rejected;
  - `listen_host` without `allowed_hosts` is rejected;
  - `host` is still loopback-only.
- **`is_allowed`,** with a fake resolver:
  - IP-literal match and mismatch;
  - a name resolving to the peer;
  - a name resolving elsewhere;
  - one name failing while another matches;
  - a slow resolver hitting the 2 s bound (use a short injected bound in tests);
  - an IPv4-mapped peer;
  - loopback always allowed;
  - case-insensitive names.
- **Server,** with an injected allow-checker or peer address, since tests can't originate
  from a non-loopback IP:
  - a refused peer gets `not_allowed` and the connection closes;
  - `_active` is unchanged;
  - no `spin_down` and no `reset_slew` call;
  - a concurrent allowed client is unaffected;
  - an allowed peer is served normally;
  - `busy` still works between two allowed peers;
  - an empty list means no check.
- **CLI:**
  - remote mode binds `listen_host`;
  - the start-up line keeps the exact prefix;
  - the unresolvable-name warning is logged.
- **Existing app tests** (`tests/test_apps_*`) pass unchanged.

## Manual check (on the lab LAN)

1. With `allowed_hosts` empty, a lab PC is refused at the TCP level (nothing is listening on
   the LAN).
2. Add a lab PC by DNS name and restart. That PC connects, `read` works and `set` controls
   power. On disconnect the trainer spins down.
3. An unlisted PC gets `not_allowed`, while a session from an allowed PC is unaffected.
4. An IP entry works.
5. Wahoo Server and Dashboard on the server PC still work, and FTMS shows ready.

## Left to the builder

- Where the protocol reference lives (README section or a separate doc).
- Internal names and how the allow-checker is injected for tests.
- Whether the start-up resolution warning runs concurrently or one name at a time.
