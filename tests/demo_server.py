"""A tiny local HR-style site used by the tests (and handy for trying the
tool by hand: `python tests/demo_server.py` then open http://127.0.0.1:8899/form.html)."""
from __future__ import annotations

import json
import sys
import threading
from email import policy
from email.parser import BytesParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

SITE = Path(__file__).parent / "demo_site"


class DemoServer:
    def __init__(self, port: int = 0) -> None:
        self.submissions: List[Dict[str, Any]] = []
        server = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(SITE), **kw)

            def log_message(self, *args):  # keep test output clean
                pass

            def do_POST(self):
                if self.path != "/api/empleados":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                msg = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body
                )
                fields: Dict[str, Any] = {}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    filename = part.get_filename()
                    if filename is not None:
                        fields[name] = {"filename": filename, "size": len(part.get_payload(decode=True) or b"")}
                    else:
                        raw = part.get_payload(decode=True) or b""
                        fields[name] = raw.decode("utf-8").strip()
                server.submissions.append(fields)
                payload = json.dumps({"id": f"EMP-{len(server.submissions):04d}"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "DemoServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    srv = DemoServer(port).start()
    print(f"Demo en {srv.url}/form.html  (Ctrl+C para salir)")
    try:
        srv._thread.join()
    except KeyboardInterrupt:
        srv.stop()
        for s in srv.submissions:
            print(s)
