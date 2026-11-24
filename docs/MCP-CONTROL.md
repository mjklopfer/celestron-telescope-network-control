# MCP interface

The optional local stdio server exposes application backends to an MCP client.
It does not operate the GUI window or share its in-memory state. An endpoint
lease prevents overlapping local GUI/MCP/bridge sessions against the same
telescope. Disconnect one before using another. Remote phone controllers are
not covered by that local lease and must be disconnected separately.

## Installation

In the project's virtual environment:

```console
python -m pip install -e ".[mcp]"
python -m cosmos_mcp
```

An MCP client launches the latter command as a stdio subprocess. Configure the
absolute virtual-environment Python path, arguments `-m cosmos_mcp`, and environment
`COSMOS90GT_HOST` with the telescope's current address. The installed console
entry point is `cosmos90gt-mcp`. A running stdio server waits for MCP messages;
it is not a terminal command prompt or a GUI. Session output stays in logs rather
than corrupting the stdio protocol. `COSMOS90GT_DATA_DIR` controls the data location.

Use `set_endpoint` while idle to change the destination. Query `status` and
`control_capabilities` before control operations. Tools return structured state;
long operations return a job ID to poll with `job_status`. Diagnostic jobs can be
aborted with `abort_diagnostics` while retaining partial results.

## Tool families

| Tools | Purpose |
| --- | --- |
| status, control_capabilities, set_endpoint | State, capability negotiation, destination |
| local_network_interfaces, discover | Adapter inventory and module discovery |
| run_diagnostics, job_status, abort_diagnostics | Allowlisted reads and cancellable jobs |
| motor_connect, motor_jog, motor_stop, motor_disconnect | Production bounded manual motion and feedback |
| controller_settings, controller_backlash_delta | Read encoder/backlash; explicit confirmed writes |
| test_ntp_server, set_wifly_time | Computer SNTP test and deliberate WiFly clock configuration |
| wifi_settings, restart_adapter | Provisioning/readback and explicit legacy-module restart |
| bridge_start, bridge_stop | Single-controller relay |
| calibration_info, estimate_relative_move | Inspect calibration; calculate a timed estimate without moving |
| position_control_start/status/arm/stop, position_reference, position_target | Experimental bounded native-target control |
| target_stream_start/status/stop | Authenticated local target ingress, simulation by default |

Movement and writes require their explicit confirmation flags. Those flags
represent the caller's authorization; they are not autonomous permission for an
AI to move a telescope. Wi-Fi credentials are read from a dedicated environment
variable rather than supplied in tool arguments. Read the tool schema for its
required variable prefix. Do not put secrets in chat, command history or logs.

Motor jogs require direction +/-1, speed 1–9 and a bounded duration 0.1–5 seconds.
Status reports telemetry and events; read age and connection state rather than
assuming a command acknowledgement means successful movement. Stop applies to
the service's own controller, not an independently running GUI or phone app.

## Streaming boundary

`target_stream_start` defaults to simulation and returns a loopback `/target`
URL and a token-file path. Producers read that local file and send
`Authorization: Bearer <token>` with JSON sequence, UTC, azimuth and elevation.
Do not expose the loopback service to the LAN by forwarding its port. Stop removes
the token file. A desktop process cannot guarantee hard real-time scheduling.

Native targets require explicit experimental authorization and, until device
commissioning succeeds, `allow_unverified_hardware=true`. This override is for
supervised commissioning, not routine use. The default remains blocked on the
current device. Travel is limited to at most 5 degrees from the initial encoder
position, and reference plus explicit arming are required before motion.
The input stream may update quickly, but the hardware loop uses the
`dispatch_rate_hz` selected in `position_control_start`. Its conservative default
is 4 Hz. The accepted range is 1–20 Hz; values above 4 Hz are explicitly
experimental and unverified, and 15–20 Hz are stress-test requests rather than
throughput promises. Actual serialized exchanges can be slower.
No variable-velocity or arrive-by-time promise is implemented.
See [tracking design](TRACKING-DESIGN.md) for stale-input, recovery and validation.

## Verified versus pending

The stdio tool-list/readback path and production timed pulses have been exercised.
Offline tests cover guards, packet construction, simulated stream rejection and
worker behavior. A later supervised synthetic hardware-loop test obtained fresh
feedback, sent bounded native targets through the Pi bridge, observed both axes
and verified Stop/disarm with stationary encoder samples. Actual encoder/
backlash writes and WiFly clock synchronization remain unverified on this telescope.
An AI should report these limits to its operator rather than infer support from
a tool name or a successful TCP connection.

The backlash-delta tool requires the previously read positive/negative settings,
signed integer deltas, and explicit write authorization. It rechecks source
values under the endpoint lease and rejects a changed baseline before writing.
GUI local references are separate session state; MCP experimental position
references are not shared with the GUI. The general HTTP companion API is
**not implemented**; only tracking-target ingress currently uses HTTP. See
[development roadmap](DEVELOPMENT-ROADMAP.md).

## Desktop 1.1 profile boundary

The desktop now selects legacy SkyQLink, modern Celestron module, or controller-proxy diagnostics profiles. This GUI selection is not shared automatically with a separate MCP process. Existing MCP operations retain their documented backend contracts; modern provisioning, HomeBrew configuration and complete GUI-profile parity are not newly exposed as MCP tools by this release. Do not infer support from the desktop controls. Use one owner of the telescope endpoint at a time.
