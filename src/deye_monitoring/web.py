"""HTTP endpoints serving Prometheus metrics and a health probe."""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deye_monitoring.collector import Collector
from deye_monitoring.exporter import render

LOGGER = logging.getLogger(__name__)


class MonitoringServer(ThreadingHTTPServer):
    """Threading HTTP server that owns a reference to the collector."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], collector: Collector) -> None:
        super().__init__(address, MetricsHandler)
        self.collector = collector


class MetricsHandler(BaseHTTPRequestHandler):
    """Serve ``/metrics`` and ``/health``; everything else is a 404."""

    server: MonitoringServer
    server_version = "deye-monitoring/0.1"

    def do_GET(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            body = render(self.server.collector.latest()).encode("utf-8")
            self._respond(200, "text/plain; version=0.0.4; charset=utf-8", body)
        elif path == "/health":
            sample = self.server.collector.latest()
            age = time.time() - sample.last_success if sample.last_success else None
            payload = {
                "ok": sample.error is None,
                "serial": sample.serial,
                "age_seconds": round(age, 1) if age is not None else None,
                "last_success": sample.last_success,
                "error": sample.error,
            }
            body = (json.dumps(payload) + "\n").encode("utf-8")
            self._respond(200 if payload["ok"] else 503, "application/json", body)
        else:
            self._respond(404, "text/plain; charset=utf-8", b"not found\n")

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)


def parse_listen(value: str) -> tuple[str, int]:
    """Parse ``host:port`` into a tuple, defaulting the host to 0.0.0.0."""
    if ":" not in value:
        return "0.0.0.0", int(value)
    host, port = value.rsplit(":", 1)
    return host or "0.0.0.0", int(port)


def create_server(listen: str, collector: Collector) -> MonitoringServer:
    """Create (but do not start) the HTTP server."""
    host, port = parse_listen(listen)
    return MonitoringServer((host, port), collector)
