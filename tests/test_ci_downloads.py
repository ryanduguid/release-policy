"""The CI download commands accept HTTPS but refuse a redirect to HTTP."""

from __future__ import annotations

import shlex
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CIDownloadTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("curl") and shutil.which("openssl"), "curl and openssl required")
    def test_ci_commands_refuse_https_to_http_redirects(self) -> None:
        commands = [shlex.split(line.strip()) for line in
                    (ROOT / ".github/workflows/ci.yml").read_text().splitlines()
                    if line.strip().startswith("curl ")]
        self.assertEqual(2, len(commands))
        plain_requests = []

        class PlainHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                plain_requests.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        plain = ThreadingHTTPServer(("127.0.0.1", 0), PlainHandler)

        class SecureHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{plain.server_port}/artifact")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b"verified fixture\n")))
                    self.end_headers()
                    self.wfile.write(b"verified fixture\n")

            def log_message(self, *_args):
                pass

        secure = ThreadingHTTPServer(("127.0.0.1", 0), SecureHandler)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Disposable fixture material, generated here and never uploaded or retained.
            cert, key = root / "fixture.pem", root / "fixture-key.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                 "-keyout", str(key), "-out", str(cert)],
                check=True, capture_output=True,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            secure.socket = context.wrap_socket(secure.socket, server_side=True)
            threads = [threading.Thread(target=server.serve_forever, daemon=True)
                       for server in (plain, secure)]
            try:
                for thread in threads:
                    thread.start()
                for command in commands:
                    for route in ("artifact", "redirect"):
                        args = command[:-1] + [f"https://127.0.0.1:{secure.server_port}/{route}",
                                              "--cacert", str(cert), "--max-time", "5"]
                        result = subprocess.run(args, cwd=root, capture_output=True, text=True,
                                                timeout=10)
                        if route == "artifact":
                            self.assertEqual(0, result.returncode, result.stderr)
                            self.assertEqual(b"verified fixture\n", (root / route).read_bytes())
                        else:
                            self.assertNotEqual(0, result.returncode)
                            self.assertIn("disabled", result.stderr)
                            self.assertEqual([], plain_requests)
            finally:
                for server in (plain, secure):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
