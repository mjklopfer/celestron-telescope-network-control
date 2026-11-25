"""Single-client AUX relay for a Version 1 Celestron WiFly adapter.

Provides later-module-style discovery and limited read-only management. One worker
owns the upstream stream; other control sessions must remain disconnected."""
import argparse
import datetime as dt
import queue
import ipaddress
import json
import math
import select
import socket
import threading
import time
import uuid
from celestron_probe import Recorder, FrameParser
from endpoint_lock import EndpointLease
from motor_control import AXES, angle, relative_angle, motor_packet, version
from setup_backend import validate_endpoint, check_direct_route
from network_info import route_source_address
from bridge_decode import describe_aux

IDENTITY = 'COSMOSBridge-AMW007-0.1.0.0, Python proxy'



class Bridge:
    def __init__(self, host, port=2000, callback=lambda event: None, bind_host='',
                 listen_port=2000, management_port=3000, advertise=True, allow_direct_upstream=False,
                 socket_factory=socket.create_connection, heartbeat_enabled=False,
                 heartbeat_interval=5.0, monitor_enabled=False, monitor_interval=1.0):
        """Initialize Bridge state; external work starts only in the corresponding operation."""
        self.host, self.port = validate_endpoint(host, port)
        address = ipaddress.ip_address(self.host)
        direct = address in ipaddress.ip_network('1.2.3.0/24')
        if direct and (not allow_direct_upstream or not bind_host):
            raise ValueError('Direct telescope link requires two-interface mode and an explicit computer router-LAN IP')
        if address.version != 4 or (not address.is_private and not direct):
            raise ValueError('Bridge requires an infrastructure/private IPv4 telescope address')
        self.bind_host = bind_host or route_source_address(self.host, self.port)
        if direct and ipaddress.ip_address(self.bind_host) in ipaddress.ip_network('1.2.3.0/24'):
            raise ValueError('Phone-facing bridge address must be on a separate router LAN, not SkyQLink')
        bind_address = ipaddress.ip_address(self.bind_host)
        if bind_address.version != 4 or not bind_address.is_private or bind_address.is_unspecified or bind_address.is_multicast:
            raise ValueError('Select a specific local IPv4 address for the bridge')
        if (self.bind_host, listen_port) == (self.host, self.port):
            raise ValueError('Bridge listener and telescope endpoint must differ')
        self.listen_port, self.management_port = listen_port, management_port
        self.advertise = advertise
        self.callback = callback
        self.socket_factory = socket_factory
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.recorder = None
        self.event_queue = queue.SimpleQueue()
        self.event_thread = None
        self.telemetry_dropped = 0
        self.recording_failed = False
        self.upstream = self.client = self.manager = None
        self.client_peer = self.manager_peer = None
        self.listeners = []
        self.udp = None
        self.parser = FrameParser()
        self.client_parser = FrameParser()
        self.local_lock = threading.Lock()
        self.local_requested = False
        self.local_enabled = False
        self.local_pending = None
        self.local_stop_requested = False
        self.local_deadline = None
        self.local_feedback = {}
        self.latest_positions = {}
        self.local_fault = False
        self.local_feedback_timeout = 2.0
        self.local_feedback_interval = .4
        self.local_reenable_at = None
        self.local_recovery_since = None
        self.local_recovery_counts = {}
        self.local_generation = 0
        self.heartbeat_enabled = False
        self.heartbeat_interval = 5.0
        self.monitor_enabled = False
        self.monitor_interval = 1.0
        self.last_client_activity = time.monotonic()
        self.last_mount_activity = 0.0
        self.last_background_query = 0.0
        self.background_pending = {}
        self.position_request_context = {}
        self.configure_background_reads(heartbeat_enabled, heartbeat_interval, monitor_enabled, monitor_interval)
        self.origins, self.firmware = {}, {}
        # A locally administered identity identifies this proxy, not the RN-171.
        self.mac = ':'.join(f'{b:02X}' for b in b'\x02' + uuid.uuid4().bytes[:5])
        self.report = {'host': self.host, 'port': self.port, 'experimental': True,
                       'ios_compatibility_verified': False, 'clients': [], 'errors': [],
                       'bytes_to_mount': 0, 'bytes_to_client': 0, 'stop_attempts': []}
        self.lease = EndpointLease(self.host, self.port)

    def emit(self, kind, **fields):
        """Queue observations without disk IO, decoding or UI callbacks on relay."""
        if kind in ('bridge_packet', 'bridge_values') and self.event_queue.qsize() >= 8192:
            self.telemetry_dropped += 1
            return
        fields.setdefault('utc', dt.datetime.now(dt.timezone.utc).isoformat())
        fields.setdefault('monotonic', time.monotonic())
        self.event_queue.put((kind, fields))

    def dispatch_events(self):
        """One consumer preserves event order; slow logs never hold the socket worker.

        Cap queued high-volume telemetry at 8192. Lifecycle/safety notices are not
        discarded. If storage/UI cannot keep up, report omitted telemetry explicitly
        rather than blocking traffic or growing an unlimited packet backlog.
        Timestamps are captured by emit(), not when the writer eventually flushes.
        """
        reported_drops = 0
        while True:
            item = self.event_queue.get()
            if item is None:
                break
            kind, fields = item
            if kind == 'bridge_packet':
                try:
                    fields['interpretation'] = describe_aux(bytes.fromhex(fields['hex']))
                    label = 'This control app' if fields.get('direction') == 'local' else 'External control app' if fields.get('direction') == 'client' else 'Controller (AUX 0x20)'
                    fields['interpretation'] = fields['interpretation'].replace('Control app', label)
                except Exception as error:
                    fields['interpretation'] = f'Interpretation unavailable: {error}'
            if self.recorder and not self.recording_failed:
                try:
                    self.recorder.record(kind, **fields)
                except Exception as error:
                    self.report['logging_error'] = str(error)
                    self.recording_failed = True
                    try:
                        self.callback({'event': 'bridge_status', 'message': f'Bridge log writing failed: {error}; relay continues.'})
                    except Exception:
                        pass
            try:
                self.callback({'event': kind, **fields})
            except Exception as error:
                self.report['callback_error'] = str(error)
            if self.telemetry_dropped != reported_drops:
                reported_drops = self.telemetry_dropped
                warning = {'message': f'Logging/display backlog: {reported_drops} telemetry events omitted; relay continues.'}
                if self.recorder and not self.recording_failed:
                    try:
                        self.recorder.record('bridge_log_overflow', **warning)
                    except Exception as error:
                        self.report['logging_error'] = str(error)
                try:
                    self.callback({'event': 'bridge_status', **warning})
                except Exception:
                    pass
        self.report['telemetry_events_dropped'] = self.telemetry_dropped

    def packet_route(self, direction, forwarded_to=None):
        local_ip = self.report.get('upstream_local', (self.bind_host, 0))[0]
        if direction == 'local':
            return dict(origin='this_program', source_ip=local_ip, destination_ip=self.host,
                        route=f'This control app ({local_ip}) → Telescope ({self.host}:{self.port})')
        if direction == 'client':
            peer = self.client_peer[0] if self.client_peer else 'unknown'
            return dict(origin='external_client', source_ip=peer, destination_ip=self.host,
                        route=f'External control app ({peer}) → This control app ({local_ip}) → Telescope ({self.host}:{self.port})')
        destination = forwarded_to[0] if forwarded_to else local_ip
        return dict(origin='telescope', source_ip=self.host, destination_ip=destination,
                    route=(f'Telescope ({self.host}:{self.port}) → This control app ({local_ip}) → External control app ({destination})'
                           if forwarded_to else f'Telescope ({self.host}:{self.port}) → This control app ({local_ip}); not forwarded'))

    def remember_position_request(self, frame, origin, requester_ip, purpose):
        if len(frame) == 6 and frame[3] in AXES and frame[4] == 1:
            key = (frame[3], frame[2])
            pending = self.position_request_context.setdefault(key, [])
            pending.append((time.monotonic(), origin, requester_ip, purpose))
            del pending[:-8]

    def position_reply_context(self, frame):
        if len(frame) != 9 or frame[2] not in AXES or frame[4] != 1:
            return {}
        pending = self.position_request_context.pop((frame[2], frame[3]), [])
        candidates = {item[1:] for item in pending if time.monotonic() - item[0] <= 10}
        if len(candidates) == 1:
            origin, requester_ip, purpose = candidates.pop()
            return dict(request_origin=origin, requester_ip=requester_ip, request_purpose=purpose,
                        request_context=f'Likely response to {purpose} requested by {origin} ({requester_ip}); inferred, AUX has no transaction IDs')
        return dict(request_context=('Request source ambiguous: overlapping requests; AUX has no transaction IDs'
                                     if candidates else 'Request source unknown: no recent matching request observed'))

    def send_local(self, frame, purpose='Local motor-control request'):
        """Send first, then queue local-origin trace; no decoding/disk IO on relay."""
        self.upstream.sendall(frame)
        route = self.packet_route('local')
        self.remember_position_request(frame, 'this control app', route['source_ip'], purpose)
        self.emit('bridge_packet', direction='local', purpose=purpose, hex=frame.hex(), **route)

    def start(self):
        """Start the relay worker; construction alone never opens a telescope connection."""
        if self.thread is not None:
            raise RuntimeError('Bridge has already been started')
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        """Signal bridge shutdown; the worker attempts Stop and drains replies before closing."""
        self.stopping.set()

    def set_local_jog(self, enabled):
        """Enable/disable worker-mediated local jogging; never write a socket from UI."""
        if not self.ready.is_set():
            raise RuntimeError('Wait until Control Bridge is ready.')
        with self.local_lock:
            self.local_reenable_at = None
            self.local_requested = bool(enabled)
            self.local_pending = None
            self.local_stop_requested = True
            self.local_generation += 1

    def configure_local_feedback(self, seconds):
        """Bridge-local motion watchdog only; does not change polling or direct mode."""
        seconds = float(seconds)
        if not math.isfinite(seconds) or not 1 <= seconds <= 10:
            raise ValueError('Bridge jog feedback timeout must be 1–10 seconds.')
        with self.local_lock:
            if seconds <= self.local_feedback_interval:
                raise ValueError('Feedback timeout must be longer than the local-jog polling interval.')
            self.local_feedback_timeout = seconds
        self.emit('bridge_local_policy', message=f'Bridge jog feedback timeout applied: {seconds:g} seconds.')

    def configure_local_polling(self, seconds):
        """Set the shared scheduler's bridge-local jog feedback interval."""
        seconds = float(seconds)
        if not math.isfinite(seconds) or not .2 <= seconds <= 5:
            raise ValueError('Local-jog polling interval must be 0.2–5 seconds.')
        with self.local_lock:
            if seconds >= self.local_feedback_timeout:
                raise ValueError('Local-jog polling interval must be shorter than the feedback timeout. Increase the timeout in Motor control first.')
            self.local_feedback_interval = seconds
            self.local_recovery_since = None
        self.emit('bridge_local_polling', interval=seconds,
                  message=f'Local-jog feedback polling interval applied: {seconds:g} seconds.')

    def restart_local_jog(self):
        """User-requested OFF for one second, then ON; never restart movement."""
        if not self.ready.is_set() or self.stopping.is_set():
            raise RuntimeError('Restart unavailable: Control Bridge is not ready.')
        with self.local_lock:
            if not self.local_requested or not self.local_enabled:
                raise RuntimeError('Enable local jogging before restarting it.')
            self.local_requested = False
            self.local_pending = None
            self.local_stop_requested = True
            self.local_generation += 1
            # Worker starts the delay after processing the OFF transition.
            self.local_reenable_at = math.inf

    def jog(self, axis, direction, rate, seconds):
        """Queue one expiring jog. No repeats, buffered sequences or hold renewal."""
        if axis not in AXES or direction not in (-1, 1):
            raise ValueError('Select a valid motor axis and direction.')
        if isinstance(rate, bool) or not isinstance(rate, int) or not 1 <= rate <= 9:
            raise ValueError('Speed must be a whole number from 1 to 9.')
        if not math.isfinite(seconds) or not .1 <= seconds <= 5:
            raise ValueError('Bridge jog duration must be 0.1–5 seconds.')
        with self.local_lock:
            if (not self.ready.is_set() or not self.local_requested or not self.local_enabled
                    or self.local_fault or self.local_stop_requested):
                raise RuntimeError('Enable local jogging and wait for fresh motor feedback.')
            if self.local_pending or self.local_deadline is not None:
                raise RuntimeError('Wait for the current jog to stop.')
            self.local_pending = (axis, direction, rate, seconds, time.monotonic(), self.local_generation)

    def stop_local_jog(self):
        """Priority request cancels any pending jog, including on external Stop."""
        with self.local_lock:
            self.local_pending = None
            self.local_stop_requested = True
            self.local_generation += 1

    def local_tick(self, now):
        """Only the relay worker calls this; bounded motion never blocks relay IO."""
        with self.local_lock:
            if self.local_reenable_at is not None and now >= self.local_reenable_at:
                self.local_reenable_at = None
                if self.ready.is_set() and not self.stopping.is_set():
                    self.local_requested = True
            desired = self.local_requested
            stop = self.local_stop_requested
            self.local_stop_requested = False
            pending = self.local_pending
            self.local_pending = None
            generation = self.local_generation
            feedback_timeout = self.local_feedback_timeout
            feedback_interval = self.local_feedback_interval
        transition = desired != self.local_enabled
        if transition or stop or (self.local_deadline is not None and now >= self.local_deadline):
            self.send_stops()
            self.local_deadline = None
            self.emit('bridge_local_motion', moving=False, message=('Local jogging locked after missing motor feedback. Waiting for fresh readings from both axes; bridge relay remains active.' if self.local_fault else 'Local jog stopped. Bridge relay remains active; external control commands are still forwarded.'))
        if transition:
            self.local_enabled = desired
            with self.local_lock:
                if not desired and self.local_reenable_at == math.inf:
                    self.local_reenable_at = now + 1.0
            self.local_fault = False
            # Preserve fragmented external frames across toggle changes. Only
            # local requests are cancelled; external commands still pass in order.
            self.local_feedback.clear()
            self.local_recovery_since = None
            self.emit('bridge_local_mode', enabled=desired,
                      message='Local jogging enabled. External commands have priority and can interrupt a jog. Hold is limited to 2 seconds.' if desired else
                      'Local jogging disabled. Pending local commands discarded; external commands continue.')
        fresh = all(now - self.local_feedback.get(axis, -math.inf) < feedback_timeout for axis in AXES)
        if self.local_enabled and self.local_deadline is not None and not fresh:
            self.send_stops()
            self.local_deadline = None
            self.local_fault = True
            self.local_recovery_since = None
            pending = None
            self.emit('bridge_local_motion', moving=False, message=f'Motor feedback exceeded {feedback_timeout:g} seconds: Stop attempted; local jogging locked. Waiting for fresh readings from both axes. Bridge relay remains active.')
        if self.local_fault:
            # Recovered feedback unlocks future button presses, never an old jog.
            # Require new samples on BOTH axes across a stable one-second window.
            pending = None
            recovering = self.ready.is_set() and not self.stopping.is_set() and all(now - self.local_feedback.get(a, -math.inf) < min(feedback_timeout, feedback_interval + .6) for a in AXES)
            if not recovering:
                self.local_recovery_since = None
            elif self.local_recovery_since is None:
                self.local_recovery_since = now
                self.local_recovery_counts = dict(self.local_feedback)
            stable = (recovering and self.local_recovery_since is not None
                      and now - self.local_recovery_since >= max(1.0, feedback_interval)
                      and all(self.local_feedback[a] > self.local_recovery_counts.get(a, -math.inf) for a in AXES))
            if stable:
                self.local_fault = False
                self.local_deadline = None
                self.local_recovery_since = None
                self.emit('bridge_local_recovered', moving=False, message='Motor feedback restored. Jog buttons are available again. Press a direction to move.')
        if pending and self.local_enabled and not self.local_fault and not stop and not transition:
            axis, direction, rate, seconds, queued, queued_generation = pending
            if fresh and now - queued < .5 and generation == queued_generation:
                # Recheck priority Stop immediately before dispatch.
                with self.local_lock:
                    if self.local_stop_requested or not self.local_requested:
                        return
                    self.send_local(motor_packet(axis, 0x24 if direction > 0 else 0x25, bytes((rate,))), purpose='Local jog')
                    self.local_deadline = time.monotonic() + seconds
                self.emit('bridge_local_motion', moving=True, message=f'Local jog: {AXES[axis]}, speed {rate}, maximum {seconds:g}s.')
            else:
                self.emit('bridge_local_motion', moving=False, message='Jog discarded: feedback is stale or request expired. Wait for fresh values and try again.')
    def configure_background_reads(self, heartbeat_enabled=False, heartbeat_interval=5.0,
                                   monitor_enabled=False, monitor_interval=1.0):
        """Configure optional read-only traffic; no socket writes from UI/API callers."""
        heartbeat_interval, monitor_interval = float(heartbeat_interval), float(monitor_interval)
        if not math.isfinite(heartbeat_interval) or not 1 <= heartbeat_interval <= 300:
            raise ValueError('Heartbeat interval must be 1–300 seconds.')
        if not math.isfinite(monitor_interval) or not .2 <= monitor_interval <= 300:
            raise ValueError('Position recording interval must be 0.2–300 seconds.')
        with self.local_lock:
            self.heartbeat_enabled = bool(heartbeat_enabled)
            self.heartbeat_interval = heartbeat_interval
            self.monitor_enabled = bool(monitor_enabled)
            self.monitor_interval = monitor_interval
        if hasattr(self, 'report'):
            settings = dict(heartbeat_enabled=self.heartbeat_enabled, heartbeat_interval=self.heartbeat_interval,
                            monitor_enabled=self.monitor_enabled, monitor_interval=self.monitor_interval)
            self.report['background_reads'] = settings
            self.emit('bridge_background_config', **settings)

    def background_read_tick(self, now):
        """One nonblocking scheduler owns all bridge-generated encoder reads.

        Reuse external replies and pending external reads before injecting anything.
        The no-client path uses these same settings: there is no hidden 1 Hz poll
        loop or three-missed-polls disconnect. Local jog readiness/safety requires
        fresh feedback at a configurable interval (default 0.4 s), using this scheduler rather than a second timer.
        AUX has no transaction IDs. Concurrent queries from the same source cannot
        be paired unambiguously, so a reply satisfies the pending read for its axis.
        Missing optional reads are logged as gaps; local_tick separately stops an
        active jog if feedback is stale. Never infer a confirmed stop from a timeout.
        """
        for axis, deadline in list(self.background_pending.items()):
            if now >= deadline:
                del self.background_pending[axis]
                self.emit('bridge_background_read', message=f'{AXES[axis]} position query received no reply within 2 seconds; sample missing.')
        if self.client_parser.buffer:
            return
        with self.local_lock:
            heartbeat, quiet_interval = self.heartbeat_enabled, self.heartbeat_interval
            monitor, sample_interval = self.monitor_enabled, self.monitor_interval
            local_interval = self.local_feedback_interval
        quiet_due = heartbeat and now - max(self.last_client_activity, self.last_mount_activity,
                                             self.last_background_query) >= quiet_interval
        interval = min(sample_interval if monitor else math.inf,
                       local_interval if self.local_enabled else math.inf)
        axes = [axis for axis in AXES if axis not in self.background_pending and
                (quiet_due or now - self.local_feedback.get(axis, -math.inf) >= interval
                 and math.isfinite(interval))]
        if not axes:
            return
        reason = 'Local-jog safety feedback' if self.local_enabled else ('Keep connection alive / heartbeat' if quiet_due else 'Position polling / recording')
        for axis in axes:
            self.send_local(motor_packet(axis, 1), purpose=reason)
            self.background_pending[axis] = now + 2.0
        self.last_background_query = now
        self.emit('bridge_background_read', message=reason + ': requested ' + ', '.join(AXES[a] for a in axes) + ' encoder positions.', axes=axes)

    def forward_client(self, raw):
        """External commands always pass. Potential conflicts cancel local motion first.

        This is command arbitration, not independent TCP writers or synthetic ACKs.
        Unknown commands conservatively preempt a jog, but remain transparent to
        the external application. No cached command or tracking rate is replayed.
        """
        self.last_client_activity = time.monotonic()
        frames = self.client_parser.feed(raw)
        for frame in frames:
            axis, command = frame[3:5]
            stop = axis in AXES and command in (0x24, 0x25) and frame[5:-1] == b'\x00'
            with self.local_lock:
                guarded = self.local_enabled or self.local_requested
            # Conservative read allowlist. Unknown commands are NOT assumed safe.
            read = command == 0xfe and len(frame) == 6
            read |= axis in AXES and command in (1, 5, 0x13, 0x40, 0x41, 0x47) and len(frame) == 6
            if guarded and (stop or not read):
                with self.local_lock:
                    interrupted = self.local_pending is not None or self.local_deadline is not None
                    self.local_pending = None
                    self.local_deadline = None
                    self.local_generation += 1
                if interrupted:
                    self.send_stops()
                    self.emit('bridge_local_motion', moving=False,
                              message=f'Local jog interrupted by external command 0x{command:02X} to 0x{axis:02X}. External command forwarded. Pause tracking in the external app if it repeatedly interrupts jogging.')
            self.upstream.sendall(frame)
            self.remember_position_request(frame, 'external client', self.client_peer[0] if self.client_peer else 'unknown', 'External-app position request')
            self.report['bytes_to_mount'] += len(frame)
            if axis in AXES and command == 1 and len(frame) == 6:
                self.background_pending.setdefault(axis, time.monotonic() + 2.0)
        self.observe(raw, 'client', frames=frames)

    def observe(self, raw, direction, frames=None, forwarded_to=None):
        """Log valid AUX frames and update readbacks from controller position replies."""
        if direction == 'mount' and raw:
            self.last_mount_activity = time.monotonic()
        parser = self.parser if direction == 'mount' else self.client_parser
        if frames is None:
            frames = parser.feed(raw)
        for frame in frames:
            # Log checksummed AUX frames, not arbitrary ASCII/configuration data.
            self.emit('bridge_packet', direction=direction, hex=frame.hex(), monotonic=time.monotonic(),
                      **self.packet_route(direction, forwarded_to),
                      **(self.position_reply_context(frame) if direction == 'mount' else {}))
            axis = frame[2]
            if direction != 'mount' or axis not in AXES or frame[3] != 0x20:
                continue
            data = frame[5:-1]
            if frame[4] == 0xfe:
                self.firmware[axis] = version(data)
            if frame[4] == 1 and len(data) == 3:
                self.background_pending.pop(axis, None)
                self.local_feedback[axis] = time.monotonic()
                self.latest_positions[axis] = {'encoder': int.from_bytes(data, 'big'), 'angle': angle(int.from_bytes(data, 'big')), 'observed_monotonic': self.local_feedback[axis]}
                count = int.from_bytes(data, 'big')
                self.origins.setdefault(axis, count)
                self.emit('bridge_values', axis=axis, encoder=count, angle=angle(count),
                          relative=relative_angle(count, self.origins[axis]),
                          firmware=self.firmware.get(axis, 'Unknown'), monotonic=time.monotonic())
        return frames

    def request(self, axis, command, timeout=1.2):
        """Send a controller query and wait within its reply deadline."""
        self.send_local(motor_packet(axis, command), purpose='Bridge startup / verification')
        deadline = time.monotonic() + timeout
        while not self.stopping.is_set() and time.monotonic() < deadline:
            try:
                raw = self.upstream.recv(4096)
            except socket.timeout:
                continue
            if not raw:
                raise ConnectionError('Telescope closed the bridge connection')
            for frame in self.observe(raw, 'mount'):
                if frame[2:5] == bytes((axis, 0x20, command)):
                    return frame[5:-1]
        return None

    def send_stops(self, axes=AXES):
        """Attempt Stop on the requested axes and record unconfirmed delivery."""
        for axis in axes:
            try:
                self.send_local(motor_packet(axis, 0x24, b'\x00'), purpose='Stop')
                self.report['stop_attempts'].append({'axis': axis, 'sent': True})
            except Exception as error:
                self.report['stop_attempts'].append({'axis': axis, 'sent': False, 'error': str(error)})
                message = f'{AXES[axis]} Stop delivery unconfirmed: {error}'
                self.report['errors'].append(message)
                self.emit('bridge_error', message=message)
        self.emit('bridge_status', message='Stop attempted on both axes; encoder verification is separate.')

    def close_client(self, reason="control app closed the socket or a socket error occurred"):
        """Drop the app socket, reset its parser and attempt motor Stop."""
        if self.client:
            self.stop_local_jog()
            self.client.close()
            self.client = None
            self.client_peer = None
            self.background_pending.clear()
            self.client_parser = FrameParser()
            self.send_stops()
            self.emit('bridge_client', message=f'App client disconnected ({reason}); Stop attempted. Waiting for a new client.')

    def proxy_identity(self):
        """Read-only software identity; never forwarded to AUX or WiFly."""
        return {'schema': 1, 'product': 'cosmos90gt-control', 'role': 'software_proxy',
                'physical_module': False, 'instance_id': self.mac,
                'version': IDENTITY, 'data_port': self.listen_port,
                'management_port': self.management_port,
                'ready': self.ready.is_set() and not self.stopping.is_set()}

    def management_reply(self, command):
        # This is a proxy, never a WiFly provisioning or firmware-update gateway.
        """Answer supported read-only proxy commands; reject configuration writes."""
        values = {'wifi.mode': '1', 'wlan.dhcp.enabled': '1',
                  'wlan.static.ip': self.bind_host, 'softap.ssid': 'COSMOS-Bridge'}
        if command == 'version':
            return IDENTITY
        if command == 'get cosmos.telemetry':
            return json.dumps({'read_only': True, 'source': 'cached received AUX replies; no new motor query',
                               'positions': {str(axis): dict(value, age_seconds=max(0, time.monotonic() - value['observed_monotonic'])) for axis, value in self.latest_positions.items()}})
        if command == 'get cosmos.proxy':
            return json.dumps(self.proxy_identity())
        if command == 'get all':
            return '\r\n'.join(f'{key}: {value}' for key, value in values.items())
        if command.startswith('get ') and command[4:] in values:
            return values[command[4:]]
        return 'ERR Unsupported proxy command; configure the telescope outside Bridge mode'

    def listen(self, port):
        """Bind a TCP listener to the selected computer interface address."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listeners.append(server)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if hasattr(socket, 'SO_EXCLUSIVEADDRUSE') else socket.SO_REUSEADDR, 1)
        server.bind((self.bind_host, port))
        server.listen(2)
        return server

    def route_still_matches_listener(self):
        """Return whether the telescope route still uses the bound LAN address."""
        try:
            return route_source_address(self.host, self.port) == self.bind_host
        except OSError:
            return False

    def run(self):
        """Own the upstream socket, relay one client, advertise discovery and clean up Stop replies."""
        try:
            self.recorder = Recorder('bridge')
            self.event_thread = threading.Thread(target=self.dispatch_events, daemon=True)
            self.event_thread.start()
            self.emit('bridge_starting', message='Opening infrastructure bridge and verifying motor replies…',
                      log_directory=str(self.recorder.directory))
            self.lease.acquire()
            check_direct_route(self.host)
            data_server = self.listen(self.listen_port)
            management_server = self.listen(self.management_port)
            self.listen_port = data_server.getsockname()[1]
            self.management_port = management_server.getsockname()[1]
            self.upstream = self.socket_factory((self.host, self.port), 3)
            self.upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.upstream.settimeout(.1)
            end = time.monotonic() + .6
            while not self.stopping.is_set() and time.monotonic() < end:
                try:
                    if not self.upstream.recv(4096):
                        raise ConnectionError('Telescope closed during startup')
                except socket.timeout:
                    pass
            for axis in AXES:
                self.request(axis, 0xfe)
                count = self.request(axis, 1)
                if self.stopping.is_set():
                    return
                if count is None or len(count) != 3:
                    raise ConnectionError('Bridge not ready: encoder replies missing; disconnect other telescope clients')
            if self.stopping.is_set():
                return
            if self.advertise:
                self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                self.udp.bind((self.bind_host, 0))
            self.ready.set()
            self.report.update(bind_host=self.bind_host, listen_port=self.listen_port,
                               management_port=self.management_port, proxy_mac=self.mac,
                               upstream_local=self.upstream.getsockname())
            self.emit('bridge_ready', message=f'Bridge ON — Control app connects to {self.bind_host}:{self.listen_port}',
                      **{k: self.report[k] for k in ('bind_host', 'listen_port', 'management_port', 'upstream_local')})
            next_advert = 0
            manager_buffer = bytearray()
            while not self.stopping.is_set():
                now = time.monotonic()
                if self.udp and self.client is None and now >= next_advert:
                    packet = json.dumps({'mac': self.mac, 'version': IDENTITY, 'cosmos_proxy': self.proxy_identity()}).encode()
                    try:
                        self.udp.sendto(packet, ('255.255.255.255', 55555))
                    except OSError as error:
                        self.emit('bridge_status', message=f'Discovery broadcast failed: {error}')
                        if not self.route_still_matches_listener():
                            raise ConnectionError(
                                f'Local Bridge address {self.bind_host} is no longer the '
                                'telescope route address; restart on the current LAN address')
                    next_advert = now + 1
                sockets = self.listeners + [self.upstream]
                sockets += [s for s in (self.client, self.manager) if s]
                readable, _, _ = select.select(sockets, [], [], .05)
                for source in readable:
                    if source in self.listeners:
                        connection, peer = source.accept()
                        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        connection.settimeout(.2)
                        if source is data_server:
                            if self.client is not None:
                                connection.close()
                                self.emit('bridge_status', message='Rejected a second motor-control client')
                            else:
                                self.client = connection
                                self.client_peer = peer
                                self.last_client_activity = time.monotonic()
                                self.report['clients'].append(peer[0])
                                self.emit('bridge_client', message=f'Client {peer[0]} connected; AUX traffic forwarding')
                        elif self.manager is not None:
                            connection.close()
                        else:
                            self.manager = connection
                            self.manager_peer = peer
                            manager_buffer.clear()
                    elif source is self.upstream:
                        raw = source.recv(4096)
                        if not raw:
                            raise ConnectionError('Telescope disconnected')
                        forwarded_to = None
                        if self.client:
                            try:
                                self.client.sendall(raw)
                                forwarded_to = self.client_peer
                                self.report['bytes_to_client'] += len(raw)
                            except OSError as error:
                                self.close_client(f'writing to control app failed: {error}')
                        self.observe(raw, 'mount', forwarded_to=forwarded_to)
                    elif source is self.client:
                        try:
                            raw = source.recv(4096)
                        except OSError as error:
                            self.close_client(f'reading from control app failed: {error}')
                            continue
                        if not raw:
                            self.close_client('control app closed its TCP stream (EOF)')
                        else:
                            self.forward_client(raw)
                    elif source is self.manager:
                        try:
                            raw = source.recv(4096)
                            if not raw:
                                raise ConnectionError('Management client disconnected')
                            manager_buffer.extend(raw)
                            if len(manager_buffer) > 4096:
                                raise ConnectionError('Management command too long')
                            while b'\n' in manager_buffer:
                                line, _, manager_buffer = manager_buffer.partition(b'\n')
                                command = line.decode('ascii', 'replace').strip()
                                # Log operation name only, never potential password arguments.
                                operation = command.split(' ', 1)[0]
                                self.emit('bridge_management', source_ip=self.manager_peer[0] if self.manager_peer else 'unknown', destination_ip=self.bind_host, operation=operation if operation in
                                          ('get', 'set', 'version', 'save', 'reset', 'exit') else 'unsupported')
                                if command == 'exit':
                                    raise ConnectionError('Management exit')
                                source.sendall((self.management_reply(command) + '\r\n> ').encode())
                        except OSError:
                            source.close()
                            self.manager = None
                self.local_tick(time.monotonic())
                self.background_read_tick(time.monotonic())
                # The bridge has no client/management inactivity lease. A quiet
                # controller may retain its connection indefinitely. Direct motor
                # idle release and local-jog feedback/deadline guards are separate.
        except Exception as error:
            message = str(error)
            if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionRefusedError)):
                message = (f'Telescope {self.host}:{self.port} closed or refused the connection ({error}). '
                           'Disconnect any control apps from the original adapter (for example SkySafari, SkyPortal or CPWI). '
                           'Start Bridge first, then connect the control app to this computer in Access Point mode. '
                           'If it still fails with other clients disconnected, the adapter may need a power-cycle.')
            self.report['errors'].append(message)
            self.emit('bridge_error', message=message)
        finally:
            self.ready.clear()
            if self.upstream:
                try:
                    self.send_stops()
                    # Drain final replies before closing the legacy RN-171 stream.
                    deadline = time.monotonic() + .8
                    replies = set()
                    last_rx = time.monotonic()
                    retried = False
                    while time.monotonic() < deadline:
                        if not retried and deadline - time.monotonic() < .5:
                            missing = [axis for axis in AXES if axis not in replies]
                            if missing:
                                self.send_stops(missing)
                                self.report['cleanup_retry_axes'] = missing
                            retried = True
                        try:
                            raw = self.upstream.recv(4096)
                        except socket.timeout:
                            if len(replies) == 2 and time.monotonic() - last_rx >= .1:
                                break
                            continue
                        if not raw:
                            break
                        last_rx = time.monotonic()
                        for frame in self.observe(raw, 'mount'):
                            if frame[2] in AXES and frame[3:5] == b'\x20\x24':
                                replies.add(frame[2])
                    self.report['cleanup_stop_replies'] = {str(a): a in replies for a in AXES}
                    self.upstream.shutdown(socket.SHUT_WR)
                except Exception as error:
                    self.report['errors'].append('Cleanup: ' + str(error))
            for connection in [self.client, self.manager, self.upstream, self.udp] + self.listeners:
                if connection:
                    try:
                        connection.close()
                    except OSError:
                        pass
            self.lease.release()
            if self.event_thread is not None:
                self.event_queue.put(None)
                self.event_thread.join()  # Sockets are closed; drain logs before report.
            directory = str(self.recorder.directory) if self.recorder else ''
            if self.recorder:
                try:
                    self.recorder.finish(self.report)
                except Exception as error:
                    self.callback({'event': 'bridge_error', 'message': f'Cannot save bridge report: {error}'})
            self.callback({'event': 'bridge_off', 'log_directory': directory,
                           'stop_replies': self.report.get('cleanup_stop_replies', {})})


def main():
    """Parse command-line options and run this module’s entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Telescope infrastructure IP')
    parser.add_argument('--port', type=int, default=2000)
    parser.add_argument('--bind', default='', help='Computer LAN IP (default: route to telescope)')
    parser.add_argument('--two-interfaces', action='store_true', help='Allow a directly attached SkyQLink upstream; requires --bind router-LAN IP')
    parser.add_argument('--heartbeat', action='store_true', help='Enable optional position-read heartbeat during quiet periods, with or without a control client')
    parser.add_argument('--heartbeat-interval', type=float, default=5.0, help='Quiet interval and heartbeat spacing in seconds (1–300; default 5)')
    parser.add_argument('--record-positions', action='store_true', help='Request missing encoder samples with or without a connected control client')
    parser.add_argument('--position-interval', type=float, default=1.0, help='Position recording interval in seconds (0.2–300; default 1; subsecond experimental)')
    args = parser.parse_args()
    bridge = Bridge(args.host, args.port, bind_host=args.bind, allow_direct_upstream=args.two_interfaces,
                    callback=lambda e: print(e, flush=True), heartbeat_enabled=args.heartbeat,
                    heartbeat_interval=args.heartbeat_interval, monitor_enabled=args.record_positions,
                    monitor_interval=args.position_interval)
    bridge.start()
    try:
        while bridge.thread.is_alive():
            bridge.thread.join(.2)
    except KeyboardInterrupt:
        bridge.stop()
        bridge.thread.join(10)


if __name__ == '__main__':
    main()
