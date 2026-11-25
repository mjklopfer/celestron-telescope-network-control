# Celestron Telescope Network Control

<img src="assets/icons/cosmos90gt.png" alt="Celestron telescope icon with WiFi on the telescope body" width="128" height="128">

**Telescope control over Wi-Fi (WiFi), using direct/ad hoc and infrastructure networks, with specialized V1 WiFly / SkyQLink RN-171 support for the Celestron COSMOS 90GT WiFi Telescope.**

A cross-platform Python desktop application for motor control, encoder readback,
module discovery, diagnostics and Wi-Fi setup. An optional bridge lets compatible
modern Celestron control applications communicate with legacy SkyQLink hardware.

[User guide](docs/USER-GUIDE.md) · [Runtime options](docs/RUNTIME-OPTIONS.md) ·
[Raspberry Pi desktop setup](docs/RASPBERRY-PI-DESKTOP.md) ·
[Pi vision and hardware-loop test](docs/PI-VISION-STACK.md) ·
[Desktop icons and shortcut support](docs/DESKTOP-ICONS-AND-SHORTCUTS.md) ·
[Version 1.1 changes](docs/RELEASE-1.1.md) · [Validation](docs/VALIDATION.md)

[![Python checks](https://github.com/mjklopfer/celestron-telescope-network-control/actions/workflows/checks.yml/badge.svg)](https://github.com/mjklopfer/celestron-telescope-network-control/actions/workflows/checks.yml)

## Current interface

![Celestron Telescope Network Control with Control Bridge enabled](docs/screenshots/network-control-1.1.png)

Current Windows interface with **Control Bridge ON** using the SkyQLink profile
on an infrastructure network. The green connection indicator confirms fresh
telescope feedback; the yellow Motor control marker identifies bridge operation.
IP addresses shown belong to this example session; use your own addresses.

## Supported telescope and Wi-Fi hardware

| Communications profile | Available functions | Verification boundary |
| --- | --- | --- |
| **SkyQLink** — Celestron V1 WiFly / RN-171 | Direct motor control, WiFly provisioning, diagnostics and optional Control Bridge | Physical baseline: COSMOS 90GT, WiFly 2.40-CEL |
| **Celestron module** — V2 or later | Direct AUX motor control, supported settings, diagnostics and experimental modern provisioning; bridge disabled | Simulator-tested; not universally verified on commercial modules |
| **Controller proxy (diagnostic)** | Proxy identity and available cached diagnostic data | No motor writes, provisioning writes or chained bridge |

The ESP32 simulator is a development stand-in for modern Celestron network
interfaces. Simulated movement is not physical-mount validation. See
[profile behavior and limits](docs/INTERFACE-MODES.md).

## Requirements

- Python **3.10 or later**, including Tkinter/Tcl-Tk and pip.
- `psutil` **5.9–7.x**, installed automatically with the package.
- A graphical desktop on Windows, macOS or Linux; Raspberry Pi OS needs an
  X desktop for the GUI. Headless services are documented separately.
- A reachable telescope Wi-Fi interface. Direct mode requires joining the
  telescope network; infrastructure mode requires a shared reachable network.
- Git to clone/update the source. Internet access is needed to download software,
  but is not required for local telescope control.

The basic GUI does not require KStars, INDI, MCP, a camera or an ESP32.
On Debian/Raspberry Pi OS, install `python3-tk` and `python3-venv` for a supported
system Python. Buster's Python 3.7 is too old; use the
[documented separate Python installation](docs/RASPBERRY-PI-DESKTOP.md) or a newer OS.

### Platform testing status

| Platform | Recorded checks |
| --- | --- |
| Windows 11 / Python 3.14.4 | Current GUI review, offline tests, startup smoke and package build |
| Raspberry Pi OS / Raspbian 10 Buster, armv7l / Python 3.12.16 | Current installation, startup, focused offline tests and desktop launch |
| macOS | Earlier working GUI/Aqua layout checks; newest UI changes require a fresh Mac review |

[Validation history](docs/VALIDATION.md) separates software checks from physical
motion tests. Earlier COSMOS/KStars two-axis motion and Stop checks do not establish
celestial alignment, GoTo accuracy or sustained satellite tracking.

## Install and run

```sh
git clone https://github.com/mjklopfer/celestron-telescope-network-control.git
cd celestron-telescope-network-control
python -m venv .venv
```

Activate the environment using the command for your shell:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```sh
# Linux / macOS
source .venv/bin/activate
```

Then install and start:

```sh
python -m pip install -e .
python -m skyqlink_setup --zoom-controls
```

Use `python3` instead of `python` where your system requires it. The equivalent
installed command is `cosmos90gt --zoom-controls`. The package and command names
remain stable for compatibility; the displayed application name is
**Celestron Telescope Network Control**.

For an existing checkout, run `git pull --ff-only` from its actual directory,
then reinstall with `python -m pip install -e .`. An older checkout directory
can remain in place; new installations use the name shown above.

### Smaller screens and optional zoom (including Raspberry Pi)

```sh
python -m skyqlink_setup --zoom-controls --ui-scale 0.8
```

The optional View menu offers Zoom in/out, Actual size and Fit width. Startup
scale accepts 0.5–2.0. See the [complete runtime reference](docs/RUNTIME-OPTIONS.md)
for every supported option, defaults, examples and when not to use it.

For a Linux desktop shortcut with 80%/100%/standard-view right-click actions,
run this from the installed environment:

```sh
python tools/install_linux_shortcut.py
```

The installer uses the current checkout and interpreter paths and backs up
existing launchers. Menu visibility depends on the desktop/file manager. See
[Pi desktop instructions](docs/RASPBERRY-PI-DESKTOP.md) and [shared icons](assets/icons/README.md).

## Connect to the telescope

1. Connect this computer to the network that can reach the telescope. Select
   **Direct / ad hoc** or **Infrastructure** to match that arrangement.
2. Click **Discover SkyQLink / Celestron Modules**, choose the responding module,
   review the recommended communications profile, then **Apply Module & Profile**.
   Alternatively, enter the telescope's confirmed IPv4 address and data port.
3. **Module & Profile Selected** applies the target and profile; it does not connect.
   Click **Connect Motors** on Motor control, or turn **Control Bridge ON** for
   an external control application through SkyQLink.
4. Check connection status and fresh encoder feedback before moving.
   Use **Disconnect** on Motor control or switch **Control Bridge OFF** to release
   the corresponding connection.

For SkyQLink only, turning Bridge ON during a direct motor connection first stops
both axes and releases that connection. Bridge startup proceeds only after both
Stop replies and socket release; failed handoff leaves the bridge off.

**View Local Network Interfaces** shows this computer's adapters, addresses,
subnets and status in every profile. Those addresses are not the telescope IP.
Discovery automatically searches supported attached private IPv4 /24 or narrower
networks; Direct mode checks 1.2.3.4 on the attached direct network. For unsupported
subnets or an undiscovered device, use the telescope address from your router's
device list. Discovery does not establish motor health or change Wi-Fi settings.

The indicator beside the profile dropdown shows green **Connected -- Link Confirmed**,
orange **Connected -- Link Unconfirmed**, or red **Disconnected**. Motor Disconnect
or idle release leaves orange: the target is retained, but no live socket is held.
Fresh bridge feedback restores green; terminal motor/bridge failure or switching
the bridge OFF shows red. An external control app disconnecting alone does not
mean the telescope-side bridge connection failed. There is no top connection switch.

Keep-alive/heartbeat may prevent unwanted idle disconnects on some devices.
Use **Control Bridge** for the SkyQLink bridge or **Diagnostics** for modern-module
polling settings. Direct SkyQLink motor connections have their own settings under
Motor control. Keep-alive does **not** override the user-idle release timer.

On the managed Raspberry Pi service, startup waits for a stable routed source
address before binding, enables the configured five-second Bridge heartbeat, and
uses a ten-second restart delay. If the active route changes while discovery is
running, the Bridge exits so systemd can restart it on the correct interface.
Use the application's own **Discover SkyQLink / Celestron Modules** operation as
the authoritative module search; it performs protocol checks and identifies the
device rather than merely finding an open TCP port.

Reverified 2026-11-25 after reboot: application discovery found the physical
SkyQLink/WiFly RN-171 at `192.168.1.231:2000` (WiFly `2.40-CEL`, both motor
controllers `5.24`). After Bridge restart, the management endpoint was Ready,
both encoder streams were fresh, and polling held approximately 3.998 Hz with
zero observed overruns. No physical movement was requested in that check.

## Application tabs

- **Motor control:** bounded jogs, Stop, raw encoder readbacks, optional keyboard
  control, local display references and controller angle/backlash settings.
  Supported cord-wrap and GoTo-approach options are in that settings dialog.
  Read one axis at a time; unsupported settings are unavailable.
- **Control Bridge:** legacy SkyQLink relay for an external control application,
  with optional mediated local jogging. Disabled for modern modules and proxies.
- **Diagnostics:** read-only queries, interpreted/raw replies, optional deeper
  reads, abort and logs. Modern-profile polling settings are here. WiFly clock
  controls are available only on the supported legacy profile.
- **Wi-Fi provisioning:** legacy WiFly SSID/security setup, or experimental modern
  configuration in the Celestron profile. Some emulated modules offer an optional
  captive setup page. Follow the instructions for the selected hardware.

Raw encoder angles are not aligned celestial coordinates. Local references
change only this app's display; controller encoder writes do not create a shared
sky alignment. Read [settings and alignment limits](docs/USER-GUIDE.md#position-reference-and-controller-settings)
before changing hardware values.

## Bridge: two addresses with different purposes

The telescope IP is the bridge's upstream destination. Your control application
(e.g., SkySafari, CPWI, KStars/INDI or another compatible application) connects to
**this computer's address on its shared network**, normally TCP 2000.
For software on this same computer, localhost works only if the bridge is bound
to loopback or otherwise listening there; use the displayed address.

Direct/ad hoc SkyQLink typically requires two network interfaces for bridging:
one to the telescope and one to the system running the control software.
Infrastructure mode can use one shared network. The app does not configure
hotspots, operating-system routing or firewalls. See the
[user guide](docs/USER-GUIDE.md#control-bridge) for exact steps and port details.

## Telescope Wi-Fi modes and physical switch

The verified COSMOS 90GT V1 switch instructions are LEFT for Direct/ad hoc and
RIGHT for Infrastructure. Power off before moving that physical switch. The
app's selector does not move the switch or join a network. These directions do
not apply universally to newer adapters: follow the selected model's instructions.

Legacy Direct mode normally uses SkyQLink-xx and 1.2.3.4:2000. Modern modules
commonly use Celestron-xxxx; management/discovery capabilities vary by firmware.
See [profile-specific provisioning](docs/INTERFACE-MODES.md) before sending settings.

## Logs and private configuration

**Network Transfer Log** shows locally generated actions. The bridge's
**Communications Log** also includes external-client traffic and replies, with
raw AUX hex, known interpretations and origin/routing metadata. Unknowns remain
unknown; inferred request matching is not proof of causation.

Copy as CSV exports displayed rows. Recording export selects a time range and
CSV/JSON timestamps. **Save All Logs** creates a ZIP; **Open Saved Logs** reads
JSON/JSONL or session ZIP files without replaying commands. Complete recordings
are on disk; the scrolling displays are bounded.

Data defaults to `~/.cosmos90gt`; `COSMOS90GT_DATA_DIR` overrides that location.
A calibration profile’s `source_log` is informational only. A missing original
log folder does not prevent loading its embedded samples or change where new
logs are saved: `<data directory>/logs`.
Passwords are redacted, but logs may include IPs, device identifiers and movement.
Review before sharing. The standalone `calibrate_telescope.py` can record settings
and explicitly authorized measurements into JSON; see
[configuration generation](docs/USER-GUIDE.md#generate-the-telescope-configuration).

## Development and validation

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -v
ruff check .
python -m skyqlink_setup --smoke-test
python -m build
# Linux without a display:
xvfb-run -a python -m unittest discover -v
```

Automated checks use fakes/loopback and do not command real telescope movement.
[Release notes](docs/RELEASE-1.1.md), [validation](docs/VALIDATION.md) and the
[roadmap](docs/DEVELOPMENT-ROADMAP.md) record implemented, tested and pending work.
Commercial modern-module provisioning, ESP32 0.2.3 build/flash and physical AUX
integration remain unverified. See the [ESP32 project](hardware/esp32-simulator/README.md)
and [HomeBrew research](docs/HOMEBREW-INTERFACES.md).

Optional [MCP automation](docs/MCP-CONTROL.md) and [tracking design](docs/TRACKING-DESIGN.md)
are separate capabilities; a general GUI-control HTTP API is not implemented.
The optional Pi vision dashboard includes live camera click-to-lock tracking,
a Control Bridge health panel with one-click motor-link reconnect and managed
Bridge service restart,
a display-only tracker, and an explicitly
armed, bounded synthetic hardware-loop commissioning mode; see its dedicated guide.



<small>Application note: [KStars/INDI installation and reproduction guide](docs/KSTARS-INDI-REPRODUCIBLE-SETUP.md).
Phone-hotspot operation remains unverified and depends on peer-device reachability.
The project is independent of Celestron and third-party control-app vendors.</small>

Bridge link confirmation uses incoming valid telescope AUX traffic, including
encoder replies, independently of the per-axis jogging safety checks. With
heartbeat enabled, the confirmation window is its applied interval plus three
seconds for the reply; without heartbeat it is 60 seconds. Silence makes the
indicator orange; a terminal bridge failure makes it red. Outgoing commands and
echoes of this app's requests do not confirm the link.
