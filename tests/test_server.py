import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from context_indicator.__main__ import make_handler
from context_indicator.monitor import Monitor


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Monitor(root, root), "abc"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()

    def test_local_dashboard_and_status(self):
        code, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("上下文".encode(), body)
        code, body = self.request("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["initial_thread"], "abc")

    def test_rebinding_cross_origin_and_file_paths_denied(self):
        for headers in ({"Host": "evil.test"}, {"Origin": "https://evil.test"}):
            self.assertEqual(self.request("/api/status", headers)[0], 403)
        self.assertEqual(self.request("/../monitor.py")[0], 404)


if __name__ == "__main__":
    unittest.main()
