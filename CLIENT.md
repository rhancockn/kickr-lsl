# KICKR power control: client guide

The FTMS service (`wahoo-ftms serve`, or Wahoo Server) holds a Wahoo KICKR in ERG mode and
accepts one client at a time over TCP. A client sets the target power, the ramp rate, and
reads status. The full reference is the "Wire protocol" section of `README.md`.

## Connecting

- Address: `127.0.0.1:47610` on the server PC. From another lab PC, use `<server-pc>:47610`; that
  PC must be listed in the service's `ftms.allowed_hosts` (see `README.md`, "Remote clients").
- Protocol: plain TCP, UTF-8, one JSON object per line ending in `\n`. Each request gets exactly
  one reply line. The server sends nothing until you send a request.
- Sequence:
  1. Open the socket.
  2. Send `connect` to check you are accepted.
  3. Send commands.
  4. Send `disconnect` (optional).
- **Safety:** whenever the connection closes, by `disconnect`, by closing the socket or by your
  program crashing, the trainer goes to 0 W.

## Commands

| Request | Reply (`"ok": true`) |
|---|---|
| `{"cmd":"connect"}` | `peer` (your address as the server sees it), `link` (`ready`, `reconnecting` or `closed`), `link_epoch` |
| `{"cmd":"set","watts":150}` | `target_w` (after the `max_watts` limit), `clamped`. It returns before the trainer confirms. |
| `{"cmd":"slew","w_per_s":20}` | `slew_w_per_s`. Increases ramp at this rate; 0 = immediate. Decreases are always immediate. It lasts until you disconnect. |
| `{"cmd":"read"}` | `link`, `link_epoch`, `target{requested_w, confirmed_w, pending, setpoint_w, ramp_end_t_lsl, t_lsl}`, `power{watts, t_lsl, age_s}`, `last_error`, `slew{w_per_s, update_interval_s}` |
| `{"cmd":"disconnect"}` | Nothing extra. The server replies at once, closes the connection, then spins the trainer down to 0 W. |

`t_lsl` values are LSL clock times (`pylsl.local_clock()`) on the server PC.

## Errors

An error reply is `{"ok": false, "error": "<code>", "detail": "<text>"}`.

| Code | Meaning | Connection |
|---|---|---|
| `not_allowed` | This PC is not in `ftms.allowed_hosts` (`detail` shows the address the server saw). | closed |
| `busy` | Another client is connected. | closed |
| `link_down` | The trainer is reconnecting; retry the command. | open |
| `invalid_watts` / `invalid_slew` / `bad_request` | Bad request. | open, except a request over 4096 bytes |

When `link_epoch` increases, the trainer has reconnected and is back at 0 W: send your target
again.

## Python

`KickrClient` needs only the standard library. It sends `connect` when it is created and raises
`KickrError` (with `.code` and `.detail`) on an error reply.

```python
from wahoo_lsl.ftms.client import KickrClient, KickrError

k = KickrClient("server-pc.lab.local", 47610)      # raises KickrError("not_allowed" / "busy")
print("connected as", k.peer, "link", k.link)
k.set_slew_rate(20)                                # W/s for increases; 0 = immediate
k.set_target_power(150)                            # returns the accepted target
print(k.read_power())                              # PowerReading(watts, t_lsl, age_s)
print(k.read_target_power())                       # TargetState(requested_w, confirmed_w, ...)
k.disconnect()                                     # returns at once; the server spins down
```

`k.close()` does the same as `disconnect()`. If the program exits or crashes without either,
the connection closes and the server still spins down.

## MATLAB

This uses `tcpclient` and needs no toolbox.

```matlab
t = tcpclient("server-pc.lab.local", 47610, "Timeout", 3);   % line terminator defaults to LF

r = kickr(t, struct("cmd", "connect"));                       % errors on not_allowed / busy
fprintf("connected as %s, link %s\n", r.peer, r.link);
kickr(t, struct("cmd", "slew", "w_per_s", 20));
r = kickr(t, struct("cmd", "set", "watts", 150));             % r.target_w, r.clamped
s = kickr(t, struct("cmd", "read"));                          % s.power.watts, s.target.confirmed_w
r = kickr(t, struct("cmd", "disconnect"));                    % optional; the server spins down
clear t                                                        % closing also spins down

function r = kickr(t, request)                                 % one request -> one reply
    writeline(t, jsonencode(request));
    r = jsondecode(readline(t));
    if ~r.ok, error("FTMS %s: %s", r.error, r.detail); end
end
```

Put `kickr` at the end of the script, or in its own `kickr.m` file.

`tcpclient` cannot turn off Nagle's algorithm. One request per round trip is unaffected. If you
want it off anyway, MATLAB's Java socket can do it (Java support is on by default in the
desktop):

```matlab
s = java.net.Socket('server-pc.lab.local', 47610);
s.setTcpNoDelay(true); s.setSoTimeout(3000);                    % Nagle off; 3 s read timeout
in  = java.io.BufferedReader(java.io.InputStreamReader(s.getInputStream(), 'UTF-8'));
out = java.io.PrintWriter(java.io.OutputStreamWriter(s.getOutputStream(), 'UTF-8'));
r = kickrj(in, out, struct("cmd", "connect"));
% ... same commands as above ...
s.close();                                                      % closing also spins down

function r = kickrj(in, out, request)                           % one request -> one reply
    out.print([jsonencode(request) newline]); out.flush();
    r = jsondecode(char(in.readLine()));
    if ~r.ok, error("FTMS %s: %s", r.error, r.detail); end
end
```

## Unity (C#)

Calls block for a network round trip, so run them off the main thread, for example with
`Task.Run`. Pass results back to the main thread before touching any GameObject.

```csharp
using System; using System.IO; using System.Net.Sockets; using UnityEngine;

[Serializable] public class Reply { public bool ok; public string error, detail, peer, link;
                                     public int link_epoch, target_w; }

public sealed class KickrLink : IDisposable {
    readonly TcpClient tcp; readonly StreamReader rd; readonly StreamWriter wr;

    public KickrLink(string host, int port = 47610) {
        tcp = new TcpClient(host, port) { NoDelay = true, ReceiveTimeout = 3000, SendTimeout = 3000 };
        var s = tcp.GetStream();
        rd = new StreamReader(s); wr = new StreamWriter(s) { AutoFlush = true, NewLine = "\n" };
        var r = Send("{\"cmd\":\"connect\"}");                // throws on not_allowed / busy
        Debug.Log($"FTMS: connected as {r.peer}, link {r.link}");
    }

    public Reply Send(string json) {
        wr.WriteLine(json);
        var r = JsonUtility.FromJson<Reply>(rd.ReadLine() ?? throw new IOException("closed by server"));
        if (!r.ok) throw new Exception($"FTMS {r.error}: {r.detail}");
        return r;
    }

    public int SetWatts(int w) => Send($"{{\"cmd\":\"set\",\"watts\":{w}}}").target_w;
    public void Disconnect() { Send("{\"cmd\":\"disconnect\"}"); Dispose(); }
    public void Dispose() => tcp.Dispose();                  // closing also spins down
}
```

For example, `var k = await Task.Run(() => new KickrLink("server-pc.lab.local"));`, then
`await Task.Run(() => k.SetWatts(150));`. For the nested `read` reply, use Newtonsoft.Json or add
matching `[Serializable]` classes for `target` and `power`.
