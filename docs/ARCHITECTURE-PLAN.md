# Connection backends: implemented foundation and future work

Update 2026-11-18: the GUI now selects legacy, modern-module or read-only proxy behavior. See [current capabilities](INTERFACE-MODES.md). The separation below remains the longer-term architecture plan; full capability discovery and MCP parity still require further work.

Historical plan recorded 2026-11-16 from the operator's development direction. This is planned
work, not a claim of current ESP32/ESP8266 or modern-module support.

Today the application's provisioning and control path targets the legacy
SkyQLink RN-171 / 2.40-CEL. Its Control Bridge presents selected modern
Celestron Wi-Fi discovery/management behavior to clients while forwarding AUX
to that legacy adapter.

Future ESP32/ESP8266-to-AUX work may provide another transport and additional
features. The application should eventually support both direct motor control
with either legacy SkyQLink or a modern Celestron Wi-Fi-compatible
interface. The desktop Control Bridge remains exclusive to the legacy SkyQLink path. The name is **Celestron**, not Skytron. Compatibility must be verified
for each actual implementation, rather than inferred from an advertised identity.

## Proposed separation

- Connection backend: connect/disconnect, identity, transport, packet exchange,
  telemetry freshness, recovery and capability reporting.
- Motion service: stop, position, bounded jog/target, limits, calibration and
  reference handling, independent of WiFly provisioning commands.
- Adapter settings: legacy WiFly commands only for positively identified legacy
  hardware; future firmware configuration uses a separate adapter implementation.
- Bridge role: client-facing discovery/relay configured separately from the
  telescope-facing connection backend.
- GUI and MCP: share service contracts and capabilities. A future **Connection
  type** selector chooses the backend; unsupported controls are disabled with
  a clear explanation, not silently routed through legacy commands.

Profiles should identify hardware/firmware/backend and protocol capabilities,
not only an IP address. Maintain migration/versioning for calibration and site
settings. Do not mix pulse calibration, encoder reference or device defaults
between backends without verification. Endpoint ownership remains exclusive.

## Implementation gates

1. Bring up ESP-to-AUX transport and establish voltage/electrical behavior,
   framing, command/response integrity, latency and disconnect/Stop behavior.
2. Define a capability/version handshake and test a backend service contract.
3. Verify motion and limits with supervised bounded tests; calibrate per device.
4. Add GUI connection selection and capability-dependent controls, then MCP
   reporting. Disable Control Bridge for the direct modern adapter mode; retain
   legacy regression tests.
5. Publish a tested hardware/firmware/client matrix. Shared celestial alignment,
   autonomous tracking, time/site persistence and higher-rate motion remain
   separate capabilities to establish, not assumptions of the new transport.

## Deferred: ESP32 Wi-Fi, GPS, compass and RTC accessory

Requested 2026-11-16; viable as a research prototype, not implemented or verified
on COSMOS. Combine an AUX/Wi-Fi bridge with a GNSS receiver, magnetometer/IMU
and optional battery-backed RTC. Keep transport and emulated accessories separate
capabilities that can be enabled independently.

- Emulate documented GPS/compass replies at AUX address `0xB0`: location, UTC
  date/time, validity, satellite status and compass response. The documented
  `0xA0` compass read returns only eight direction codes, not a degree heading or
  a writable setting. Offer precise heading/tilt through a separate API rather
  than inventing a new payload for the legacy command.
- Validate magnetometer calibration, tilt compensation, mounting orientation,
  magnetic interference from motors/steel, and magnetic versus true north.
  Compass plus GPS supplies initial reference information, not proven celestial
  alignment or autonomous tracking.
- Use GPS UTC to discipline the RTC; report holdover age and validity honestly
  when GPS is unavailable. Never report a retained location as a fresh fix.
- Investigate optional `0xB2` RTC emulation separately: the known interface is
  model dependent. A physical RTC does not require presenting this AUX address
  if clients only consume GPS time replies.
- Verify AUX electrical levels, bus arbitration, timing and address conflicts
  before attachment. Do not impersonate an accessory already on the bus.
- Capture actual client requests to establish whether COSMOS, a hand controller,
  or control software requests and caches this information. Supplying replies
  does not establish a writable site endpoint in the motor controllers.
- Validate separately: packet fixtures, bench electrical tests, read-only client
  compatibility, restart/no-fix behavior, then supervised alignment tests.

Protocol basis: [NexStar AUX observations, GPS/compass chapter](https://paquettefamily.ca/nexstar/NexStar_AUX_Commands_10.pdf).
This is a reverse-engineered research document, not an official Celestron
compatibility guarantee.


Candidate GNSS hardware: SEQURE M10-25Q (GNSS serial NMEA/UBX, QMC5883L
magnetometer over I2C). Manufacturer lists 5 V supply; verify UART logic and I2C
pull-up voltages on the actual board before connecting ESP32 GPIO. This is not a
complete tilt-sensing IMU; add an accelerometer/IMU for tilt compensation if the
sensor tilts with the telescope. Keep it away from motor magnetic fields.
[Manufacturer manual](https://flymod.net/download/sequre_m10_12_m10_18_m10_25q_gps_manual).


Candidate holdover clock: DS3231 temperature-compensated, battery-backed I2C RTC,
operated with ESP32-compatible 3.3 V signaling. Keep calendar registers in UTC;
check oscillator-stop/validity status before trusting retained time, discipline
from a valid GNSS time source, and expose age/source of the last synchronization.
Choose breakout pull-ups and battery circuitry appropriate to the supply and
battery; a DS3231 alone does not implement Celestron AUX.
[Analog Devices datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/DS3231.pdf).

See the [breakout-board wiring concept](ESP32-ACCESSORY-WIRING.md). Exact board
selection, connector pinout and electrical bench validation remain pending.


## Direct adapter mode decision (2026-11-16)

This is a distinct hardware/firmware subproject within the overall project.
Only after the real AUX adapter is implemented and verified, add Motor control
connection choices such as **Legacy SkyQLink (RN-171)** and **Celestron Wi-Fi
(direct ESP32 adapter)**. The ESP32 connects directly to the mount's AUX bus.
The desktop Control Bridge has no role in this mode: disable it with a clear
explanation, stop any existing bridge before switching, and never start its
listener/discovery automatically. Mode changes require disconnection and clear
session references. Keep legacy WiFly provisioning/time controls unavailable in
the direct mode; expose ESP32 settings through its own verified capability API.

The current `hardware/esp32-simulator` explicitly has no physical AUX UART/GPIO
interface. A working simulator/network connection is not evidence of electrical
or arbitration compatibility. Real AUX support and this mode selector are not
implemented by the current desktop clock update.

Before claiming compatibility, require:

1. A reviewed connector/cable netlist and power budget, 3.3 V-safe receive paths,
   inactive TX/BUSY drivers at startup/reset and power sequencing without backfeed.
2. Hardware-timed serial transmission and confirmed stop-bit configuration.
   Older AUX notes specify 19200 8N1; newer HomeBrew development notes report 8N2.
   Resolve this against the chosen implementation and real COSMOS captures;
   do not equate ordinary PC RTS/CTS with the complete AUX arbitration protocol.
3. Sense bus availability, assert/release BUSY correctly, wait the required guard
   time, complete the final stop bit before releasing TX, and handle contention,
   echoes, malformed/partial frames, bounded queues and reply deadlines.
4. GPS/RTC emulator address-conflict checks, honest validity and no fabricated
   accessory replies; transparent forwarding must not silently rewrite unrelated
   commands or claim a different motor model merely to pass a client handshake.
5. Bench scope/logic-analyzer evidence before a read-only mount test, followed by
   supervised movement/Stop, disconnect/reconnect and client-compatibility tests.
   Record hardware revision, firmware and ESP32 core versions in the test matrix.

Reference review:
- [Mark Lord's earlier ESP32 AUX implementation, archived v3.8](https://github.com/g7ltt/Celestron-GPS-WiFi-BT-Interface/blob/main/esp32_wifi-V3.8.ino):
  separate BUSY handling, timing and echo processing are explicit concerns. This
  is a historical reference, not a current compatibility guarantee.
- [HomeBrew author discussion of current UART behavior](https://www.cloudynights.com/forums/topic/854327-homebrew-gen3-pcb-wifibtgpsmusbrelay/page/84/).
- [HBG3 documentation mirror](https://github.com/Mraanderson/HBG3): the mirror
  notes restrictions on redistribution of newer source. Review permissions before
  reuse; this project has not imported that firmware. Public interface observations
  and our independently written tests should be recorded with source provenance.

No Celestron certification or complete official AUX conformance specification
has been established. Describe the result as tested interoperability with named
mount/client versions, not universal or certified compliance.

### Direct USB-to-AUX variant

A USB-to-AUX adapter is a useful alternative transport for a Raspberry Pi or
desktop tracking controller. It can remove Wi-Fi and TCP/bridge jitter, but USB
does not make a generic USB-UART cable electrically or logically compatible with
AUX. Reuse the reviewed tri-state transmitter, protected 3.3 V receive path,
BUSY/arbitration handling, checksum/framing and safe boot-state requirements in
the ESP32 design. The host backend must keep one request/reply in flight, preserve
endpoint ownership and give Stop priority. Treat 19200 framing and bus timing as
pending measurement items. Validate read-only traffic with a logic analyzer before
supervised movement. Benchmark its sustainable loop rate separately from Wi-Fi;
do not assume that removing TCP makes the motor controllers accept arbitrary rates.


## Capability-driven discovery and Motor control selection

Prefer an explicit **Auto-detect / Legacy SkyQLink / Direct Celestron Wi-Fi**
selector to an ambiguous alternate-control checkbox. The requested manual choice
is a transport preference, not permission to bypass identity checks. Discovery
may report both types; positively identify the selected adapter and obtain a
versioned capability report before enabling adapter-specific commands. Unknown
hardware remains unclassified; do not infer type solely from IP, SSID or an
AMW007-like advertisement. Never auto-switch a connected control session.

Common verified AUX reads and motion-service contracts should work through either
backend. Keep diagnostic catalogs separated into mount AUX capabilities and
adapter-specific settings. WiFly command-mode provisioning, WiFly time-server
writes and RN-171 recovery are legacy-only. Direct ESP32 diagnostics should report
its own identity, uptime/reset reason, bus status, GPS validity and RTC source/age
only when those endpoints are implemented. Disable irrelevant controls with a
short explanation; do not send WiFly commands to the new adapter. Control Bridge
is legacy-only and unavailable in direct mode. Each backend owns its own clock,
reset evidence, default snapshot and recovery policy while sharing the GUI/MCP
contracts for supported operations.

ESP32 work resume point: [decisions, wiring references and ordered TODO checklist](ESP32-RESUME.md).


### HomeBrew and alternative interfaces — research checkpoint

See [alternative interface findings and ESP32 implementation plan](HOMEBREW-INTERFACES.md).
Our ESP32 remains a Celestron-compatible module project with optional extensions.
HomeBrew location support is an unverified, opt-in developer prototype; GUI/MCP
integration and physical device validation remain pending.
