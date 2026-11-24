"""Local STDIO MCP adapter for the existing COSMOS backends; no screen automation.

Install: python -m pip install -e '.[mcp]'
Run: python cosmos_mcp.py
Set COSMOS90GT_HOST/COSMOS90GT_PORT for the default endpoint. Startup never connects
or moves hardware. The GUI and MCP are independent clients sharing the existing
cross-process endpoint lease: disconnect one before using the other. Status is
MCP-session state, not a claim that the GUI has the same connection/state.

Long operations return job IDs; job_status retrieves results and abort_diagnostics
cooperatively cancels only diagnostic jobs. Motor commands are bounded timed jogs
(0.1-5 seconds); continuous unattended movement and arbitrary AUX packets are not
exposed. Explicit confirmation flags document intent for writes/movement but do
not replace the calling assistant obtaining actual user authorization. Use motor_stop
at any time, even while a job runs. Normal EOF/shutdown requests Stop/disconnect.
Abrupt process termination or transport loss cannot guarantee Stop delivery.

No arbitrary shell/file operations are exposed. Wi-Fi passwords are read from a
named local environment variable rather than sent through MCP arguments. GUI-only
zoom/layout controls are intentionally outside this hardware interface. See
docs/MCP-CONTROL.md for setup, limitations and verification status.
"""
import asyncio
from contextlib import asynccontextmanager
from collections import deque
import os
import threading
import time
import uuid
from mcp.server.fastmcp import FastMCP
from motor_control import MotorController, AXES, angle
from bridge_mode import Bridge
from diagnostics import diagnose
from discover_modules import scan_modules
from setup_backend import operate, validate_endpoint
from motor_settings import motor_settings
from time_settings import configure_time, test_time_server
from recover_link import recover
from network_info import network_interfaces
from telescope_calibration import load_config, estimate_seconds, relative_target, identify_profile, save_config
from position_control import PositionController
from tracking_stream import TrackingStream


class ControlService:
    def __init__(self):
        self.host, self.port = validate_endpoint(os.environ.get('COSMOS90GT_HOST', '1.2.3.4'),
                                               os.environ.get('COSMOS90GT_PORT', '2000'))
        self.motor = None
        self.bridge = None
        self.position_controller = None
        self.target_stream = None
        self.events = deque(maxlen=300)
        self.jobs = {}
        self.lock = threading.RLock()

    def event(self, event):
        with self.lock:
            self.events.append(dict(event))

    def idle(self):
        if self.position_controller and self.position_controller.thread and self.position_controller.thread.is_alive():
            raise RuntimeError('Stop experimental position control first')
        if self.motor and self.motor.thread and self.motor.thread.is_alive():
            raise RuntimeError('Disconnect MCP motor control first')
        if self.bridge and self.bridge.thread and self.bridge.thread.is_alive():
            raise RuntimeError('Stop MCP bridge first')
        if any(j['status'] == 'running' for j in self.jobs.values()):
            raise RuntimeError('Wait for or abort the current operation')

    def start_job(self, operation, function, cancellable=False):
        with self.lock:
            self.idle()
            identifier = uuid.uuid4().hex
            cancel = threading.Event()
            job = {'id': identifier, 'operation': operation, 'status': 'running',
                   'cancel': cancel, 'cancellable': cancellable}
            self.jobs[identifier] = job
            def worker():
                try:
                    result = function(cancel)
                    with self.lock:
                        job.update(status='aborted' if isinstance(result, dict) and result.get('aborted') else 'complete', result=result)
                except Exception as error:
                    with self.lock:
                        job.update(status='failed', error=str(error))
            threading.Thread(target=worker, daemon=True).start()
            return {'job_id': identifier, 'status': 'running'}

    def shutdown(self):
        if self.target_stream:
            self.target_stream.stop()
        if self.position_controller:
            self.position_controller.stop()
        with self.lock:
            for job in self.jobs.values():
                job['cancel'].set()
            if self.motor:
                self.motor.disconnect()
            if self.bridge:
                self.bridge.stop()
        for owner in (self.motor, self.bridge, self.position_controller):
            if owner and owner.thread:
                owner.thread.join(12)


service = ControlService()


@asynccontextmanager
async def lifespan(server):
    try:
        yield service
    finally:
        await asyncio.to_thread(service.shutdown)


mcp = FastMCP('COSMOS90GT', lifespan=lifespan,
              instructions='Control real telescope hardware only with user authorization. One client owns the endpoint. Use status/job_status and bounded jogs; motor_stop is always available. Encoder writes are not sky alignment.')


@mcp.tool()
def status() -> dict:
    """Read this MCP session's state and recent events; no telescope requests."""
    with service.lock:
        motor = service.motor
        positions = ({AXES[a]: {'encoder': count, 'raw_degrees': angle(count),
                              'age_seconds': time.monotonic() - motor.position_times.get(a, 0)}
                      for a, count in list(motor.positions.items())} if motor else {})
        return {'endpoint': f'{service.host}:{service.port}',
                'motor_connected': bool(motor and motor.connected.is_set()),
                'bridge_ready': bool(service.bridge and service.bridge.ready.is_set()),
                'positions': positions, 'events': list(service.events)[-30:],
                'jobs': [{k: j[k] for k in ('id', 'operation', 'status')} for j in service.jobs.values()]}


@mcp.tool()
def set_endpoint(host: str, port: int = 2000) -> dict:
    """Select endpoint while idle; does not connect or change telescope settings."""
    with service.lock:
        service.idle()
        service.host, service.port = validate_endpoint(host, port)
    return status()


@mcp.tool()
def local_network_interfaces() -> list:
    """Read local network interfaces."""
    return network_interfaces()


@mcp.tool()
def discover(mode: str = 'infrastructure', timeout_seconds: int = 30) -> dict:
    """Start read-only network discovery; retrieve results with job_status."""
    if mode not in ('infrastructure', 'direct') or not 1 <= timeout_seconds <= 60:
        raise ValueError('Use infrastructure/direct and a 1-60 second timeout')
    return service.start_job('discovery', lambda cancel: scan_modules(
        port=service.port, mode=mode, timeout=timeout_seconds, callback=service.event))


@mcp.tool()
def run_diagnostics(deep: bool = False, confirm_deep: bool = False) -> dict:
    """Start read-only diagnostics. Deep model-dependent queries require explicit opt-in."""
    if deep and not confirm_deep:
        raise ValueError('Confirm intrusive deep queries first')
    def operation(cancel):
        report = diagnose(service.host, service.port, callback=service.event, deep=deep, cancel=cancel)
        try:
            profile = load_config()
            identify_profile(profile, report)
            save_config(profile)
            report['calibration_identity'] = 'Matched adapter MAC; address updated'
        except (OSError, ValueError, KeyError) as error:
            report['calibration_identity'] = str(error)
        return report
    return service.start_job('diagnostics', operation, True)


@mcp.tool()
def job_status(job_id: str) -> dict:
    """Return operation state/result, including raw/decoded diagnostic data and log path."""
    with service.lock:
        job = service.jobs[job_id]
        return {k: v for k, v in job.items() if k != 'cancel'}


@mcp.tool()
def abort_diagnostics(job_id: str) -> dict:
    """Stop new diagnostic queries, preserving partial results and transport cleanup."""
    with service.lock:
        job = service.jobs[job_id]
        if not job['cancellable']:
            raise ValueError('This operation cannot be cooperatively aborted')
        job['cancel'].set()
    return {'job_id': job_id, 'abort_requested': True}


@mcp.tool()
def motor_connect(idle_release_seconds: int = 30) -> dict:
    """Connect/poll encoders without movement; default intentional idle release is 30 seconds."""
    with service.lock:
        service.idle()
        service.motor = MotorController(service.host, service.port, service.event,
                                        idle_release=idle_release_seconds)
        service.motor.start()
    return {'status': 'connecting; use status to verify both encoders'}


@mcp.tool()
def motor_jog(axis: str, direction: int, speed: int, seconds: float,
              confirm_movement: bool = False) -> dict:
    """Request a physical bounded 0.1-5 second jog. Verify clearance and user intent first."""
    if not confirm_movement:
        raise ValueError('Explicit movement authorization required')
    address = next((a for a, name in AXES.items() if name.lower() == axis.lower()), None)
    if address is None or service.motor is None:
        raise ValueError('Select Azimuth/Elevation and connect first')
    service.motor.jog(address, direction, speed, seconds)
    return {'queued': True, 'check': 'Use status for encoder displacement and stationary verification'}


@mcp.tool()
def motor_stop() -> dict:
    """Priority Stop request. Not blocked by diagnostic/job state; no automatic restart."""
    if service.position_controller:
        service.position_controller.pause()
    if service.motor:
        service.motor.stop()
    if service.bridge:
        service.bridge.stop()
    return {'stop_requested': True, 'delivery_verified': False,
            'note': 'Only sessions owned by this MCP process can be stopped; inspect events.'}


@mcp.tool()
def motor_disconnect() -> dict:
    """Request Stop and release MCP motor ownership."""
    if service.motor:
        service.motor.disconnect()
    return {'disconnect_requested': True}


@mcp.tool()
def controller_settings(axis: str, encoder_degrees: float | None = None,
                        positive_backlash: int | None = None, negative_backlash: int | None = None,
                        confirm_write: bool = False) -> dict:
    """Read settings, or explicitly write raw encoder/backlash. NOT celestial alignment."""
    address = next((a for a, name in AXES.items() if name.lower() == axis.lower()), None)
    if address is None:
        raise ValueError('Select Azimuth or Elevation')
    changes = {cmd: value for cmd, value in ((4, encoder_degrees), (0x10, positive_backlash),
                                            (0x11, negative_backlash)) if value is not None}
    if changes and not confirm_write:
        raise ValueError('Explicit setting-write authorization required')
    return service.start_job('controller_settings', lambda cancel: motor_settings(
        service.host, service.port, address, changes=changes, callback=service.event))


@mcp.tool()
def controller_backlash_delta(axis: str, expected_positive: int, expected_negative: int,
                              positive_delta: int = 0, negative_delta: int = 0,
                              confirm_write: bool = False) -> dict:
    """Apply signed whole-number deltas to previously read backlash; reread before writing.

    Expected and resulting values must be 0–99. Units are not degrees. If the
    telescope values changed since the supplied read, nothing is written.
    """
    from motor_settings import setting_packet
    address = next((a for a, name in AXES.items() if name.lower() == axis.lower()), None)
    if address is None or not confirm_write:
        raise ValueError('Select an axis and explicitly authorize the write')
    for value in (expected_positive, expected_negative, positive_delta, negative_delta):
        if type(value) is not int:
            raise ValueError('Backlash and delta must be whole-number integers')
    for command, value in ((0x10, expected_positive), (0x11, expected_negative)):
        setting_packet(address, command, value)
    changes = {0x10: expected_positive + positive_delta, 0x11: expected_negative + negative_delta}
    for command, value in changes.items():
        setting_packet(address, command, value)
    return service.start_job('controller_backlash_delta', lambda cancel: motor_settings(
        service.host, service.port, address, changes=changes,
        expected_backlash=(expected_positive, expected_negative), callback=service.event))


@mcp.tool()
def test_ntp_server(hostname: str) -> dict:
    """Test an NTP server from this computer, not from the telescope."""
    return service.start_job('ntp_test', lambda cancel: test_time_server(hostname))


@mcp.tool()
def set_wifly_time(enable: int | None = None, server_ipv4: str | None = None,
                   sync_now: bool = False, save_all_ram_settings: bool = False,
                   confirm_write: bool = False) -> dict:
    """Set WiFly clock policy/server or sync now. 0=off, 1=startup, 2-1440=minutes.

    Saving writes ALL current RAM settings. Does not set mount alignment or location.
    """
    if not confirm_write:
        raise ValueError('Explicit clock-write authorization required')
    if enable is not None and not 0 <= enable <= 1440:
        raise ValueError('Use 0, 1, or 2-1440 minutes')
    return service.start_job('wifly_time', lambda cancel: configure_time(
        service.host, service.port, enable=enable, server_ip=server_ipv4,
        sync_now=sync_now, persist=save_all_ram_settings, callback=service.event))


@mcp.tool()
def wifi_settings(ssid: str = '', security: str = 'WPA2-Personal (AES)',
                  password_environment_variable: str = '', confirm_write: bool = False) -> dict:
    """Read settings by default. Explicit writes save provisioning; supply secrets via local env."""
    password = ''
    if confirm_write and security != 'Open (no password)':
        if not password_environment_variable.startswith('COSMOS_WIFI_'):
            raise ValueError('Supply a dedicated COSMOS_WIFI_ environment variable name')
        password = os.environ[password_environment_variable]
    return service.start_job('wifi_settings', lambda cancel: operate(
        service.host, service.port, ssid, password, security, confirm_write, callback=service.event))


@mcp.tool()
def restart_adapter(confirm_restart: bool = False) -> dict:
    """Explicit RN-171 recovery/restart; interrupts connection but retains saved Wi-Fi."""
    if not confirm_restart:
        raise ValueError('Explicit restart authorization required')
    return service.start_job('restart', lambda cancel: recover(service.host, service.port, callback=service.event))


@mcp.tool()
def bridge_start(bind_ipv4: str, two_interface_mode: bool = False) -> dict:
    """Start the experimental shared-network AUX bridge; this gives remote clients motor control."""
    with service.lock:
        service.idle()
        service.bridge = Bridge(service.host, service.port, callback=service.event,
                                bind_host=bind_ipv4, allow_direct_upstream=two_interface_mode)
        service.bridge.start()
    return {'status': 'starting; inspect status for verified readiness'}


@mcp.tool()
def bridge_stop() -> dict:
    """Stop the MCP-owned bridge and request both motor Stops."""
    if service.bridge:
        service.bridge.stop()
    return {'stop_requested': True}


@mcp.tool()
def calibration_info() -> dict:
    """Read the local telescope-specific calibration/baseline config."""
    return load_config()


@mcp.tool()
def estimate_relative_move(axis: str, speed: int, degrees: float,
                           current_encoder: int | None = None) -> dict:
    """Calculate a pulse duration/encoder target only; NEVER moves hardware."""
    address = next((a for a, name in AXES.items() if name.lower() == axis.lower()), None)
    if address is None:
        raise ValueError('Select Azimuth or Elevation')
    result = {'estimated_seconds': estimate_seconds(load_config(), address, speed, degrees,
                                                     service.host, service.port),
              'approximate': True, 'movement_sent': False}
    if current_encoder is not None:
        result['absolute_encoder_target'] = relative_target(current_encoder, degrees)
    return result



@mcp.tool()
def control_capabilities() -> dict:
    """Negotiate supported control modes; never infer tracking support from TCP connectivity."""
    return {
        'timed_jogs': {'implemented': True, 'seconds_range': [0.1, 2], 'speeds': [1, 9]},
        'telemetry': {'encoder_positions': True, 'age_seconds': True, 'connection_state': True},
        'latest_position_targets': {'implemented_experimentally': True, 'device_verified': False,
                                    'default_enabled': False, 'default_dispatch_hz': 4,
                                    'configurable_dispatch_hz_range': [1, 20],
                                    'rates_above_4_hz_unverified': True,
                                    'rate_guaranteed': False, 'max_relative_envelope_deg': 5},
        'stream_ingress': {'transport': 'authenticated loopback HTTP', 'simulation': True,
                           'hardware_requires_commissioning_override': True},
        'recovery': {'fresh_encoder_pair_required': True, 'new_target_after_reconnect_required': True,
                     'old_targets_replayed': False, 'bounded_backoff_seconds': [.5, 2]},
        'predictive_variable_velocity': False, 'arrive_by_time_guarantee': False,
        'shared_celestial_alignment': False, 'hardware_stop_sensors_verified': False,
        'safety': 'Software envelope and best-effort Stop; physical limits and lost-link Stop delivery are not guaranteed.'}


@mcp.tool()
def position_control_start(envelope_degrees: float = 5, confirm_experimental: bool = False,
                           allow_unverified_hardware: bool = False,
                           dispatch_rate_hz: float = 4) -> dict:
    """Connect experimental native encoder-target worker. No motion until reference + explicit arm.

    Confirm clear travel within the supplied envelope BOTH ways on BOTH axes.
    This build caps the envelope at 5 degrees; it is not a validated ISS/launch tracker.
    The conservative default is 4 Hz. Rates above 4 Hz are experimental; rates
    above 10 Hz are stress-test requests that may be physically unsustainable.
    """
    if not confirm_experimental:
        raise ValueError('Explicit experimental-control authorization required')
    try:
        profile = load_config()
        verified = (profile['telescope']['host'] == service.host
                    and profile.get('native_target_verification', {}).get('verified') is True)
    except (OSError, ValueError, KeyError):
        verified = False
    if not verified and not allow_unverified_hardware:
        raise RuntimeError('Native target control is not verified on this device. Use simulation; supervised commissioning requires explicit allow_unverified_hardware.')
    with service.lock:
        service.idle()
        service.position_controller = PositionController(
            service.host, service.port, envelope_degrees,
            dispatch_rate_hz=dispatch_rate_hz)
        service.position_controller.start()
    return {'status': 'connecting', 'dispatch_rate_hz': dispatch_rate_hz,
            'rate_warning': service.position_controller.rate_warning,
            'next': 'Poll position_control_status; set reference; arm explicitly.'}


@mcp.tool()
def position_control_status() -> dict:
    """Return live/stale encoder positions, last target, connection, recovery and motion state."""
    return service.position_controller.status() if service.position_controller else {'connection': 'not_started'}


@mcp.tool()
def position_reference(azimuth_deg: float, elevation_deg: float) -> dict:
    """Set the worker's local pointing reference while paused; does NOT write mount encoders."""
    if not service.position_controller:
        raise RuntimeError('Start position control first')
    service.position_controller.reference(azimuth_deg, elevation_deg)
    return position_control_status()


@mcp.tool()
def position_control_arm(confirm_movement: bool = False) -> dict:
    """Arm live target execution after user confirms clearance and reference. Incoming targets MOVE hardware."""
    if not confirm_movement or not service.position_controller:
        raise ValueError('Start, reference and explicitly authorize physical movement first')
    service.position_controller.arm()
    return position_control_status()


@mcp.tool()
def position_target(azimuth_deg: float, elevation_deg: float, sequence: int, utc: float) -> dict:
    """Replace latest desired position. When armed this MOVES hardware. Unix UTC and sequence must be current.

    MCP calls are accepted rapidly but native targets use the rate configured when
    position control starts. The default is 4 Hz; faster rates are unverified,
    and requests above 10 Hz are stress tests rather than a throughput promise.
    For sustained high-rate ingestion use the authenticated loopback stream.
    """
    if not service.position_controller:
        raise RuntimeError('Start position control first')
    try:
        return service.position_controller.submit(azimuth_deg, elevation_deg, sequence, utc)
    except (ValueError, RuntimeError):
        service.position_controller.pause()
        raise


@mcp.tool()
def target_stream_start(mode: str = 'simulation', min_elevation: float = 0,
                        max_elevation: float = 80, max_rate_deg_s: float = 2.5) -> dict:
    """Start authenticated loopback POST /target ingestion. Default simulation never moves hardware.

    Experimental hardware mode forwards to an already-started position worker,
    which must separately be referenced/armed. Invalid stream input pauses/disarms it.
    """
    if service.target_stream:
        raise RuntimeError('Stop the current target stream first')
    if mode not in ('simulation', 'experimental_hardware'):
        raise ValueError('Use simulation or experimental_hardware')
    controller = service.position_controller if mode == 'experimental_hardware' else None
    if mode == 'experimental_hardware' and controller is None:
        raise RuntimeError('Start bounded experimental position control first')
    service.target_stream = TrackingStream(min_elevation, max_elevation, max_rate_deg_s,
                                          max_rate_deg_s, controller=controller)
    return service.target_stream.start()


@mcp.tool()
def target_stream_status() -> dict:
    """Read ingress and simulated/experimental controller state."""
    return service.target_stream.status() if service.target_stream else {'state': 'not_started'}


@mcp.tool()
def target_stream_stop() -> dict:
    """Close stream ingress and pause/disarm its hardware controller, if any."""
    if service.target_stream:
        service.target_stream.stop()
        service.target_stream = None
    return {'stream_stopped': True}


@mcp.tool()
def position_control_stop() -> dict:
    """Request Stop, close streaming ingress and release experimental position control."""
    target_stream_stop()
    if service.position_controller:
        service.position_controller.stop()
    return {'stop_requested': True, 'delivery_verified': False}


def main():
    os.environ['COSMOS90GT_QUIET_REPORTS'] = '1'  # STDOUT belongs exclusively to MCP JSON-RPC.
    mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
