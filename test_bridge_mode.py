"""Bridge integration tests use loopback and a simulated telescope only."""
import json
import queue
import socket
import threading
import time
import unittest
from unittest.mock import patch
from bridge_mode import Bridge
from celestron_probe import FrameParser
from endpoint_lock import EndpointLease, EndpointBusy
from motor_control import motor_packet
from test_motor_control import FakeRecorder


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(.1)
        self.halt = threading.Event()
        self.frames = []
        self.drop_positions = 0
        self.positions = {16: 12345, 17: 67890}
        self.events = queue.Queue()
        self.patch = patch('bridge_mode.Recorder', FakeRecorder)
        self.patch.start()
        self.server = threading.Thread(target=self.serve, daemon=True)
        self.server.start()
        self.bridge = Bridge('192.0.2.231', 2000, self.events.put, bind_host='127.0.0.1',
                             listen_port=0, management_port=0, advertise=False,
                             socket_factory=lambda address, timeout: socket.create_connection(self.listener.getsockname(), timeout))
        self.bridge.start()
        self.wait('bridge_ready')

    def serve(self):
        connection = None
        try:
            while not self.halt.is_set():
                try:
                    connection, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            if connection is None:
                return
            connection.settimeout(.1)
            parser = FrameParser()
            while not self.halt.is_set():
                try:
                    raw = connection.recv(4096)
                except socket.timeout:
                    continue
                if not raw:
                    break
                for frame in parser.feed(raw):
                    self.frames.append(frame)
                    axis, command = frame[3:5]
                    if command == 1 and self.drop_positions:
                        self.drop_positions -= 1
                        continue
                    data = (b'\x05\x18' if command == 0xfe else self.positions[axis].to_bytes(3, 'big')
                            if command == 1 else b'')
                    body = bytes((3 + len(data), axis, frame[2], command)) + data
                    reply = b'\x3b' + body + bytes((-sum(body) & 255,))
                    connection.sendall(reply[:2])
                    connection.sendall(reply[2:])
        except OSError:
            pass
        finally:
            if connection:
                connection.close()

    def wait(self, kind, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=.05)
            except queue.Empty:
                continue
            if event['event'] == 'bridge_error':
                self.fail(event['message'])
            if event['event'] == kind:
                return event
        self.fail('Missing event: ' + kind)

    def tearDown(self):
        self.bridge.stop()
        self.bridge.thread.join(5)
        self.assertFalse(self.bridge.thread.is_alive())
        self.halt.set()
        self.listener.close()
        self.server.join(2)
        self.patch.stop()

    def connect(self, port):
        return socket.create_connection(('127.0.0.1', port), 2)

    def test_management_discovery_does_not_interrupt_connected_controller(self):
        from discover_modules import identify_proxy
        with self.connect(self.bridge.listen_port) as client:
            self.wait('bridge_client')
            before = len(self.frames)
            result = identify_proxy('127.0.0.1', self.bridge.listen_port,
                                    management_port=self.bridge.management_port)
            self.assertEqual(result['endpoint_kind'], 'software_proxy')
            self.assertEqual(result['port'], self.bridge.listen_port)
            self.assertTrue(result['proxy_identity']['ready'])
            self.assertEqual(len(self.frames), before)
            packet = motor_packet(16, 1)
            client.sendall(packet)
            self.await_frame(packet)
            self.assertTrue(self.bridge.thread.is_alive())

    def test_forwarding_split_packets_encoder_readback_and_no_competing_poll(self):
        with self.connect(self.bridge.listen_port) as client:
            client.settimeout(2)
            self.wait('bridge_client')
            # Let an idle poll already in flight finish before checking ownership.
            time.sleep(.2)
            before = len(self.frames)
            time.sleep(1.2)
            self.assertEqual(len(self.frames), before)
            packet = motor_packet(16, 1)
            client.sendall(packet[:3])
            client.sendall(packet[3:])
            parser = FrameParser()
            deadline = time.monotonic() + 2
            reply = None
            while time.monotonic() < deadline and reply is None:
                for frame in parser.feed(client.recv(4096)):
                    if frame[2:5] == b'\x10\x20\x01':
                        reply = frame
            self.assertIsNotNone(reply)
            self.assertEqual(int.from_bytes(reply[5:-1], 'big'), 12345)
            self.assertIn(packet, self.frames)
        self.wait('bridge_client')
        deadline = time.monotonic() + 1
        while not self.bridge.report['stop_attempts'] and time.monotonic() < deadline:
            time.sleep(.01)
        while not any(f[4:6] == b'\x24\x00' for f in self.frames) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(any(f[4:6] == b'\x24\x00' for f in self.frames))

    def test_quiet_control_and_management_connections_survive_former_cutoffs(self):
        with self.connect(self.bridge.listen_port) as client, self.connect(self.bridge.management_port) as manager:
            self.wait('bridge_client')
            time.sleep(.2)  # Finish any startup poll before the quiet interval.
            stops_before = len(self.bridge.report['stop_attempts'])
            time.sleep(16)  # Beyond both removed 15s/10s cutoffs; loopback only.
            self.assertIsNotNone(self.bridge.client)
            self.assertIsNotNone(self.bridge.manager)
            self.assertEqual(len(self.bridge.report['stop_attempts']), stops_before)
            manager.settimeout(2)
            manager.sendall(b'version\n')
            self.assertIn(b'AMW007', manager.recv(4096))
            client.settimeout(2)
            client.sendall(motor_packet(16, 1))
            parser = FrameParser()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if any(f[2:5] == b'\x10\x20\x01' for f in parser.feed(client.recv(4096))):
                    break
            else:
                self.fail('No position reply after quiet interval')

    def test_relay_disables_tcp_small_packet_batching(self):
        with self.connect(self.bridge.listen_port):
            self.wait('bridge_client')
            self.assertEqual(self.bridge.upstream.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
            self.assertEqual(self.bridge.client.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)

    def test_blocked_log_writer_does_not_block_command_or_reply_relay(self):
        release = threading.Event()
        entered = threading.Event()
        original = self.bridge.recorder.record
        def slow_record(kind, **fields):
            if kind == 'bridge_packet':
                entered.set()
                release.wait(5)
            original(kind, **fields)
        with self.connect(self.bridge.listen_port) as client:
            self.wait('bridge_client')
            self.bridge.recorder.record = slow_record
            try:
                client.settimeout(1)
                parser = FrameParser()
                for axis in (16, 17):
                    client.sendall(motor_packet(axis, 1))
                    deadline = time.monotonic() + 1
                    received = False
                    while time.monotonic() < deadline:
                        if any(f[2:5] == bytes((axis, 32, 1)) for f in parser.feed(client.recv(4096))):
                            received = True
                            break
                    self.assertTrue(received, 'Disk logging held up telescope traffic')
                    self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
            finally:
                release.set()

    def test_position_recording_captures_external_motion_without_jogging(self):
        with self.connect(self.bridge.listen_port):
            self.wait('bridge_client')
            self.positions = {16: 234567, 17: 765432}
            before = len(self.frames)
            self.bridge.configure_background_reads(False, 5, True, .2)
            seen = set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(seen) < 2:
                event = self.wait('bridge_values')
                if event['encoder'] == self.positions[event['axis']]:
                    seen.add(event['axis'])
            self.assertEqual(seen, {16, 17})
            self.assertFalse(self.bridge.local_enabled)
            self.assertTrue(all(f[4] == 1 for f in self.frames[before:]))

    def test_missing_optional_samples_keep_bridge_alive_and_recover(self):
        self.drop_positions = 2
        self.bridge.configure_background_reads(False, 5, True, .2)
        event = self.wait('bridge_background_read', timeout=5)
        while 'sample missing' not in event['message']:
            event = self.wait('bridge_background_read', timeout=5)
        self.wait('bridge_values', timeout=5)
        self.assertTrue(self.bridge.thread.is_alive())

    def test_no_hidden_idle_polls_when_both_options_off(self):
        before = len(self.frames)
        time.sleep(1.3)
        self.assertEqual(len(self.frames), before)
        self.assertTrue(self.bridge.thread.is_alive())

    def test_endpoint_exclusion_off_stops_and_releases(self):
        other = EndpointLease('192.0.2.231', 2000)
        with self.assertRaises(EndpointBusy):
            other.acquire()
        self.bridge.stop()
        self.wait('bridge_off')
        self.bridge.thread.join(2)
        self.assertEqual(self.bridge.report['cleanup_stop_replies'], {'16': True, '17': True})
        with other:
            pass
        self.assertTrue(all(any(f[3] == axis and f[4:6] == b'\x24\x00' for f in self.frames) for axis in (16, 17)))

    def test_management_read_only_and_credentials_not_logged(self):
        with self.connect(self.bridge.management_port) as client:
            client.settimeout(2)
            client.sendall(b'get all\n')
            raw = client.recv(4096)
            self.assertIn(b'wlan.dhcp.enabled: 1', raw)
            client.sendall(b'set wlan.passkey secret-to-never-log\n')
            self.assertIn(b'ERR Unsupported', client.recv(4096))
        self.assertNotIn('secret-to-never-log', repr(self.bridge.recorder.events))
        self.assertFalse(any(f[4] in (0x24, 0x25) and f[5] > 0 for f in self.frames))

    def test_second_client_is_rejected(self):
        with self.connect(self.bridge.listen_port):
            self.wait('bridge_client')
            with self.connect(self.bridge.listen_port) as second:
                second.settimeout(2)
                self.assertEqual(second.recv(1), b'')

    def await_frame(self, packet):
        deadline = time.monotonic() + 2
        while packet not in self.frames and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIn(packet, self.frames)

    def test_external_tracking_preempts_local_jog_before_forwarding(self):
        with self.connect(self.bridge.listen_port) as client:
            self.wait('bridge_client')
            self.enable_local()
            self.bridge.jog(16, 1, 2, 2)
            self.assertTrue(self.wait('bridge_local_motion')['moving'])
            body = bytes((6, 0x20, 17, 7, 0, 0, 87))
            tracking = b'\x3b' + body + bytes((-sum(body) & 255,))
            start = len(self.frames)
            client.sendall(tracking)
            event = self.wait('bridge_local_motion')
            self.assertFalse(event['moving'])
            self.assertIn('interrupted', event['message'])
            self.await_frame(tracking)
            after = self.frames[start:]
            self.assertLess(after.index(motor_packet(16, 0x24, b'\x00')), after.index(tracking))
            self.assertLess(after.index(motor_packet(17, 0x24, b'\x00')), after.index(tracking))
            self.assertIsNone(self.bridge.local_deadline)

    def enable_local(self):
        self.bridge.set_local_jog(True)
        self.wait('bridge_local_mode')
        deadline = time.monotonic() + 2
        while len(self.bridge.local_feedback) != 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(set(self.bridge.local_feedback), {16, 17})

    def test_local_mode_relays_fragmented_external_motion_and_reads(self):
        with self.connect(self.bridge.listen_port) as client:
            client.settimeout(2)
            self.wait('bridge_client')
            self.enable_local()
            move = motor_packet(16, 0x25, b'\x06')
            client.sendall(move[:3])
            time.sleep(.05)
            client.sendall(move[3:] + motor_packet(17, 1))
            self.await_frame(move)
            parser = FrameParser()
            frames = []
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not any(f[2:5] == b'\x11\x20\x01' for f in frames):
                frames.extend(parser.feed(client.recv(4096)))
            self.assertTrue(any(f[2:5] == b'\x11\x20\x01' for f in frames))
            self.assertIn(move, self.frames)

    def test_bounded_local_jog_and_release_never_replay_external_command(self):
        with self.connect(self.bridge.listen_port) as client:
            self.wait('bridge_client')
            self.enable_local()
            blocked = motor_packet(17, 0x25, b'\x09')
            client.sendall(blocked)
            self.await_frame(blocked)
            self.bridge.jog(16, 1, 2, .1)
            event = self.wait('bridge_local_motion')
            self.assertTrue(event['moving'])
            event = self.wait('bridge_local_motion')
            self.assertFalse(event['moving'])
            self.assertIn(motor_packet(16, 0x24, b'\x02'), self.frames)
            self.assertIsNone(self.bridge.local_deadline)
            self.bridge.set_local_jog(False)
            self.wait('bridge_local_mode')
            self.assertEqual(self.frames.count(blocked), 1)
            fresh = motor_packet(17, 0x25, b'\x03')
            client.sendall(fresh)
            deadline = time.monotonic() + 1
            while fresh not in self.frames and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn(fresh, self.frames)

    def test_local_stop_cancels_pending_and_external_stop_interrupts_jog(self):
        with self.connect(self.bridge.listen_port) as client:
            self.wait('bridge_client')
            self.enable_local()
            # Atomically simulate a queued GUI request followed by priority Stop.
            with self.bridge.local_lock:
                self.bridge.local_pending = (16, 1, 8, .2, time.monotonic(), self.bridge.local_generation)
                self.bridge.local_stop_requested = True
            self.wait('bridge_local_motion')
            self.assertNotIn(motor_packet(16, 0x24, b'\x08'), self.frames)
            self.bridge.jog(17, 1, 2, 2)
            self.assertTrue(self.wait('bridge_local_motion')['moving'])
            client.sendall(motor_packet(16, 0x24, b'\x00'))
            self.assertFalse(self.wait('bridge_local_motion')['moving'])
            self.assertIsNone(self.bridge.local_deadline)

    def test_local_unknown_writes_forwarded_and_invalid_jogs_rejected(self):
        self.enable_local()
        for args in ((16, 1, 2, float('nan')), (16, 1, 2, 6), (16, 1, 0, .1), (18, 1, 2, .1)):
            with self.assertRaises(ValueError):
                self.bridge.jog(*args)
        with self.connect(self.bridge.listen_port) as client:
            self.wait('bridge_client')
            for command in (2, 4, 6, 7, 0x38, 0x3a, 0xfd):
                body = bytes((6, 0x20, 16, command, 0, 0, 1))
                packet = b'\x3b' + body + bytes((-sum(body) & 255,))
                client.sendall(packet)
                self.await_frame(packet)
                self.assertEqual(self.frames.count(packet), 1)

    def test_stale_local_feedback_discards_jog(self):
        self.enable_local()
        self.drop_positions = 100
        time.sleep(2.1)
        before = len(self.frames)
        self.bridge.jog(16, 1, 9, .1)
        event = self.wait('bridge_local_motion')
        self.assertIn('discarded', event['message'])
        self.assertNotIn(motor_packet(16, 0x24, b'\x09'), self.frames[before:])

    def test_toggle_preserves_incomplete_external_frame(self):
        with self.connect(self.bridge.listen_port) as client:
            self.wait('bridge_client')
            packet = motor_packet(16, 0x25, b'\x03')
            client.sendall(packet[:3])
            time.sleep(.1)
            # Partial app frames suppress injected queries, including jog reads.
            self.bridge.set_local_jog(True)
            self.wait('bridge_local_mode')
            client.sendall(packet[3:])
            self.await_frame(packet)
            self.assertEqual(self.frames.count(packet), 1)

    def test_feedback_loss_locks_local_motion_while_feedback_missing(self):
        self.enable_local()
        self.drop_positions = 100
        # Start with feedback old enough to expire during this bounded jog.
        for axis in (16, 17):
            self.bridge.local_feedback[axis] = time.monotonic() - 1.5
        self.bridge.jog(16, 1, 2, 2)
        self.assertTrue(self.wait('bridge_local_motion')['moving'])
        stopped = self.wait('bridge_local_motion')
        self.assertFalse(stopped['moving'])
        self.assertIn('feedback exceeded', stopped['message'])
        self.assertTrue(self.bridge.local_fault)
        with self.assertRaises(RuntimeError):
            self.bridge.jog(16, 1, 2, .1)

    def test_discovery_packet_has_proxy_identity_and_expected_udp_destination(self):
        sent = queue.Queue()
        class UDP:
            def sendto(self, payload, destination):
                sent.put((payload, destination))
            def close(self):
                pass
        self.bridge.udp = UDP()
        payload, destination = sent.get(timeout=3)
        data = json.loads(payload)
        self.assertEqual(destination, ('255.255.255.255', 55555))
        self.assertEqual(data['mac'], self.bridge.mac)
        self.assertIn('AMW007', data['version'])


class BridgeConfigurationTests(unittest.TestCase):
    def test_direct_requires_explicit_dual_interface_opt_in_and_other_lan(self):
        for options in ({}, {'allow_direct_upstream': True},
                        {'allow_direct_upstream': True, 'bind_host': '1.2.3.10'}):
            with self.assertRaises(ValueError):
                Bridge('1.2.3.4', **options)
        bridge = Bridge('1.2.3.4', allow_direct_upstream=True, bind_host='192.168.1.30')
        self.assertEqual(bridge.bind_host, '192.168.1.30')

    def test_structured_proxy_identity_is_read_only_and_reports_ports(self):
        bridge = Bridge('192.0.2.231', bind_host='127.0.0.1', listen_port=1234, management_port=1235)
        identity = json.loads(bridge.management_reply('get cosmos.proxy'))
        self.assertEqual(identity['role'], 'software_proxy')
        self.assertIs(identity['physical_module'], False)
        self.assertEqual(identity['data_port'], 1234)
        self.assertEqual(identity['management_port'], 1235)
        self.assertFalse(identity['ready'])
        bridge.ready.set()
        self.assertTrue(json.loads(bridge.management_reply('get cosmos.proxy'))['ready'])
        self.assertIsNone(bridge.upstream)

    def test_advertisement_identity_and_management_are_distinct_from_real_firmware(self):
        from bridge_mode import IDENTITY
        bridge = Bridge('192.0.2.231', bind_host='127.0.0.1')
        self.assertIn('AMW007', IDENTITY)
        self.assertTrue(bridge.mac.startswith('02:'))
        self.assertIn('ERR', bridge.management_reply('save'))
        self.assertIn('ERR', bridge.management_reply('reset'))

    def test_route_change_is_detected_for_service_restart(self):
        bridge = Bridge('192.0.2.231', bind_host='127.0.0.1')
        with patch('bridge_mode.route_source_address', return_value='192.168.1.149'):
            self.assertFalse(bridge.route_still_matches_listener())
        with patch('bridge_mode.route_source_address', return_value='127.0.0.1'):
            self.assertTrue(bridge.route_still_matches_listener())


class BackgroundReadTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        self.bridge = Bridge('192.0.2.231', bind_host='127.0.0.1')
        self.bridge.upstream = Mock()
        self.bridge.client = object()
        self.bridge.last_client_activity = 100
        self.bridge.last_background_query = 100

    def test_disabled_default_and_quiet_interval_suppression(self):
        b = self.bridge
        b.background_read_tick(110)
        b.upstream.sendall.assert_not_called()
        b.configure_background_reads(True, 5)
        b.background_read_tick(104.9)
        b.upstream.sendall.assert_not_called()
        b.background_read_tick(105)
        self.assertEqual(b.upstream.sendall.call_count, 2)
        b.last_client_activity = 106
        b.background_pending.clear()
        b.background_read_tick(110)
        self.assertEqual(b.upstream.sendall.call_count, 2)
        b.background_read_tick(111)
        self.assertEqual(b.upstream.sendall.call_count, 4)
        b.configure_background_reads(False, 5)
        b.background_read_tick(120)
        self.assertEqual(b.upstream.sendall.call_count, 4)

    def test_recording_reuses_fresh_samples_and_never_duplicates_pending_reads(self):
        b = self.bridge
        b.configure_background_reads(False, 5, True, 1)
        b.local_feedback = {16: 101, 17: 101}
        b.background_read_tick(101)
        b.upstream.sendall.assert_not_called()
        b.background_read_tick(102.1)
        self.assertEqual(b.upstream.sendall.call_count, 2)
        b.background_read_tick(103.2)
        self.assertEqual(b.upstream.sendall.call_count, 2)
        body = bytes((6, 16, 32, 1, 0, 0, 1))
        b.observe(b'\x3b' + body + bytes((-sum(body) & 255,)), 'mount')
        self.assertNotIn(16, b.background_pending)
        self.assertIn(17, b.background_pending)

    def test_invalid_intervals_and_partial_frames(self):
        b = self.bridge
        for value in (0, -1, 301, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                b.configure_background_reads(True, value)
        b.configure_background_reads(True, 1)
        b.client_parser.feed(b'\x3b\x03')
        b.background_read_tick(110)
        b.upstream.sendall.assert_not_called()


class UnifiedReadTests(BackgroundReadTests):
    def test_no_client_uses_same_optional_controls(self):
        b = self.bridge
        b.client = None
        b.background_read_tick(110)
        b.upstream.sendall.assert_not_called()
        b.configure_background_reads(False, 5, True, 10)
        b.local_feedback = {16: 100, 17: 100}
        b.background_read_tick(109)
        b.upstream.sendall.assert_not_called()
        b.background_read_tick(110)
        self.assertEqual(b.upstream.sendall.call_count, 2)

    def test_jog_feedback_reuses_external_samples_and_pending_requests(self):
        b = self.bridge
        b.local_enabled = True
        b.local_feedback = {16: 110, 17: 110}
        b.background_read_tick(110.3)
        b.upstream.sendall.assert_not_called()
        b.background_pending[16] = 112
        b.background_read_tick(110.5)
        b.upstream.sendall.assert_called_once_with(motor_packet(17, 1))
        b.background_read_tick(111)
        self.assertEqual(b.upstream.sendall.call_count, 1)

    def test_mount_traffic_satisfies_heartbeat(self):
        b = self.bridge
        b.configure_background_reads(True, 5)
        b.last_mount_activity = 108
        b.background_read_tick(112)
        b.upstream.sendall.assert_not_called()
        b.background_read_tick(113)
        self.assertEqual(b.upstream.sendall.call_count, 2)

    def test_external_request_prevents_background_duplicate(self):
        b = self.bridge
        b.configure_background_reads(False, 5, True, 1)
        b.forward_client(motor_packet(16, 1))
        b.background_read_tick(__import__('time').monotonic())
        self.assertEqual(b.upstream.sendall.call_count, 2)
        self.assertEqual(b.upstream.sendall.call_args_list[1].args[0], motor_packet(17, 1))


class LocalJogRecoveryTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        self.b = Bridge('192.0.2.231', bind_host='127.0.0.1')
        self.b.upstream = Mock()
        self.b.ready.set()
        self.b.local_enabled = self.b.local_requested = True

    def test_local_interval_validation_scheduler_and_slow_recovery(self):
        b = self.b
        for value in (.1, 6, float('nan'), 2):
            with self.assertRaises(ValueError):
                b.configure_local_polling(value)
        b.configure_local_feedback(5)
        b.configure_local_polling(2)
        with self.assertRaises(ValueError):
            b.configure_local_feedback(2)
        b.local_feedback = {16: 100, 17: 100}
        b.last_background_query = 100
        b.background_read_tick(101)
        b.upstream.sendall.assert_not_called()
        b.background_read_tick(102.1)
        self.assertEqual(b.upstream.sendall.call_count, 2)
        b.local_fault = True
        b.local_feedback = {16: 103, 17: 103}
        b.local_tick(103)
        b.local_tick(104.1)
        self.assertTrue(b.local_fault)
        b.local_feedback = {16: 105.1, 17: 105.1}
        b.local_tick(105.1)
        self.assertFalse(b.local_fault)
        self.assertIsNone(b.local_deadline)

    def test_timed_jog_accepts_five_seconds_and_rejects_longer(self):
        b = self.b
        b.jog(16, 1, 2, 5)
        self.assertEqual(b.local_pending[3], 5)
        b.local_pending = None
        for seconds in (5.01, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                b.jog(16, 1, 2, seconds)

    def test_packet_routes_distinguish_local_client_and_return_destination(self):
        b = self.b
        b.report['upstream_local'] = ('192.0.2.10', 40000)
        b.client_peer = ('192.0.2.20', 50000)
        b.send_local(motor_packet(16, 1))
        event, fields = b.event_queue.get_nowait()
        self.assertEqual(event, 'bridge_packet')
        self.assertEqual(fields['origin'], 'this_program')
        self.assertEqual(fields['source_ip'], '192.0.2.10')
        self.assertEqual(fields['destination_ip'], b.host)
        self.assertEqual(b.packet_route('client')['source_ip'], '192.0.2.20')
        self.assertEqual(b.packet_route('mount', b.client_peer)['destination_ip'], '192.0.2.20')
        self.assertIn('not forwarded', b.packet_route('mount')['route'])
        incoming = b.packet_route('client')['route']
        self.assertLess(incoming.index('External control app'), incoming.index('This control app'))
        self.assertLess(incoming.index('This control app'), incoming.index('Telescope'))
        outgoing = b.packet_route('mount', ('192.0.2.3', 12345))['route']
        self.assertLess(outgoing.index('Telescope'), outgoing.index('This control app'))
        self.assertLess(outgoing.index('This control app'), outgoing.index('External control app'))

    def test_reply_request_context_is_inferred_or_ambiguous(self):
        b = self.b
        request = motor_packet(16, 1)
        reply = bytes.fromhex('3b0610200140000089')
        b.send_local(request, purpose='Keep connection alive / heartbeat')
        context = b.position_reply_context(reply)
        self.assertIn('heartbeat', context['request_purpose'])
        self.assertIn('inferred', context['request_context'])
        b.send_local(request, purpose='Local-jog safety feedback')
        b.remember_position_request(request, 'external control app', '192.0.2.20', 'External request')
        self.assertIn('ambiguous', b.position_reply_context(reply)['request_context'])
        self.assertIn('unknown', b.position_reply_context(reply)['request_context'])

    def test_timeout_validation_and_effect(self):
        b = self.b
        for value in (0, 11, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                b.configure_local_feedback(value)
        b.configure_local_feedback(5)
        b.local_feedback = {16: 97, 17: 97}
        b.local_deadline = 110
        b.local_tick(100)
        self.assertFalse(b.local_fault)
        b.local_tick(102.1)
        self.assertTrue(b.local_fault)
        self.assertIsNone(b.local_deadline)

    def test_auto_recovery_requires_both_axes_and_never_replays_motion(self):
        b = self.b
        b.local_fault = True
        b.local_feedback = {16: 100, 17: 95}
        b.local_tick(100)
        self.assertIsNone(b.local_recovery_since)
        b.local_feedback = {16: 100, 17: 100}
        b.local_tick(100)
        b.local_feedback = {16: 100.6, 17: 100.6}
        b.local_tick(100.6)
        self.assertTrue(b.local_fault)
        b.local_feedback = {16: 101.1, 17: 101.1}
        b.local_pending = (16, 1, 9, 1, 101.1, b.local_generation)
        b.local_tick(101.1)
        self.assertFalse(b.local_fault)
        self.assertIsNone(b.local_deadline)
        self.assertIsNone(b.local_pending)
        b.upstream.sendall.assert_not_called()

    def test_restart_waits_one_second_and_clears_old_feedback(self):
        b = self.b
        b.local_feedback = {16: 100, 17: 100}
        b.local_fault = True
        b.restart_local_jog()
        b.local_tick(100)
        self.assertFalse(b.local_enabled)
        b.local_tick(100.9)
        self.assertFalse(b.local_enabled)
        b.local_tick(101)
        self.assertTrue(b.local_enabled)
        self.assertFalse(b.local_fault)
        self.assertEqual(b.local_feedback, {})
        self.assertIsNone(b.local_deadline)

    def test_explicit_off_cancels_pending_restart(self):
        b = self.b
        b.restart_local_jog()
        b.local_tick(100)
        b.set_local_jog(False)
        b.local_tick(102)
        self.assertFalse(b.local_enabled)

    def test_stop_message_preserves_feedback_lock_reason(self):
        b = self.b
        b.local_fault = True
        b.stop_local_jog()
        b.local_tick(100)
        events = []
        while not b.event_queue.empty():
            events.append(b.event_queue.get())
        self.assertTrue(any('locked after missing motor feedback' in e[1].get('message', '') for e in events))
