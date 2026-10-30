import http.client
import http.server
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import setup_quick_connect
iroh = None
import quick_connect
from quick_connect import ALPN, InvalidTicket, QuickNode


class _MockGateway(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.requests = 0
        super().__init__(("127.0.0.1", 0), _MockHandler)


class _MockHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        self.server.requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n')
        self.wfile.flush()
        if self.path == "/slow":
            time.sleep(1.0)
        try:
            self.wfile.write(b'event: response.completed\ndata: {"type":"response.completed"}\n\n')
            self.wfile.flush()
        except OSError:
            pass


class QuickConnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global iroh
        try:
            iroh = setup_quick_connect.load()
        except (OSError, ValueError):
            raise unittest.SkipTest("Install verified quick connection runtime first")
        cls.gateway = _MockGateway()
        cls.gateway_thread = threading.Thread(target=cls.gateway.serve_forever, daemon=True)
        cls.gateway_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.gateway.shutdown()
        cls.gateway.server_close()
        cls.gateway_thread.join(3)

    def setUp(self):
        self.token = "synthetic-capability-" + "x" * 32
        self.supplier = QuickNode(validator=lambda value: value == self.token,
                                  relay_mode="disabled", iroh_module=iroh)
        self.friend = QuickNode(relay_mode="disabled", iroh_module=iroh)
        self.ticket = self.supplier.start_supplier("127.0.0.1", self.gateway.server_port)

    def tearDown(self):
        self.friend.close()
        self.supplier.close()

    def _post(self, base, path="/v1/responses"):
        request = urllib.request.Request(base + path,
            data=b'{"model":"mock","stream":true}',
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()

    def test_mock_http_sse_round_trip(self):
        base = self.friend.connect_peer(self.ticket, self.token)
        body = self._post(base)
        self.assertIn(b"response.output_text.delta", body)
        self.assertIn(b"response.completed", body)
        self.friend.disconnect_peer(base)
        port = urllib.parse.urlsplit(base).port
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def test_invalid_capability_never_reaches_gateway(self):
        before = self.gateway.requests
        base = self.friend.connect_peer(self.ticket, "invalid-capability-" + "z" * 32)
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError,
                                http.client.RemoteDisconnected)):
            self._post(base)
        self.assertEqual(self.gateway.requests, before)

    def test_cancel_and_close_remove_listener(self):
        base = self.friend.connect_peer(self.ticket, self.token)
        port = urllib.parse.urlsplit(base).port
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"POST /slow HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Authorization: Bearer synthetic\r\nContent-Length: 2\r\n"
                     b"Connection: close\r\n\r\n{}")
        self.assertIn(b"200", sock.recv(4096))
        sock.close()
        self.friend.disconnect_peer(base)
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def test_rejects_non_loopback_ticket_in_disabled_mode(self):
        parsed = iroh.EndpointTicket.from_string(self.ticket)
        addr = parsed.endpoint_addr()
        hostile = iroh.EndpointAddr(addr.id(), None, ["10.0.0.8:443"])
        ticket = str(iroh.EndpointTicket.from_addr(hostile))
        with self.assertRaises(InvalidTicket):
            self.friend.connect_peer(ticket, self.token)

    def test_connection_without_first_stream_releases_supplier_slot(self):
        async def connect_without_stream():
            endpoint = await self.friend._get_endpoint()
            addr = self.friend._validated_addr(self.ticket)
            return await endpoint.connect(addr, ALPN)

        with patch.object(quick_connect, "HANDSHAKE_TIMEOUT", 0.15):
            connection = self.friend._submit(connect_without_stream(), 2)
            appeared = time.monotonic() + 1
            while not self.supplier._connection_tasks and time.monotonic() < appeared:
                time.sleep(0.01)
            self.assertTrue(self.supplier._connection_tasks)
            deadline = time.monotonic() + 2
            while self.supplier._connection_tasks and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(self.supplier._connection_tasks)
            connection.close(0, b"")


if __name__ == "__main__":
    unittest.main()
