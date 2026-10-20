"""Tests for the HTTP endpoints, exercised over a real loopback socket."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from deye_monitoring.collector import Collector
from deye_monitoring.snapshot import Reading
from deye_monitoring.web import create_server, parse_listen


class FakeReader:
    def __init__(self) -> None:
        self.fail = False

    def __call__(self) -> tuple[list[Reading], str | None]:
        if self.fail:
            raise RuntimeError("no reply")
        return [Reading("PV1 Power", 654, "W", 0x00BA, "pv")], "2601255637"


@pytest.fixture
def server():
    collector = Collector(FakeReader())
    collector.poll_once()
    httpd = create_server("127.0.0.1:0", collector)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    yield type(
        "Server",
        (),
        {
            "httpd": httpd,
            "collector": collector,
            "url": lambda _self, path: f"http://{host}:{port}{path}",
        },
    )()
    httpd.shutdown()
    httpd.server_close()


def test_parse_listen_defaults_host() -> None:
    assert parse_listen("9101") == ("0.0.0.0", 9101)
    assert parse_listen("127.0.0.1:9101") == ("127.0.0.1", 9101)


def test_metrics_endpoint_returns_exposition(server) -> None:
    with urllib.request.urlopen(server.url("/metrics")) as response:
        body = response.read().decode()
    assert response.status == 200
    assert "text/plain" in response.headers["Content-Type"]
    assert "# TYPE deye_pv1_power_watts gauge" in body
    assert 'deye_pv1_power_watts{group="pv",serial="2601255637"} 654' in body
    assert "deye_scrape_success 1" in body


def test_health_endpoint_is_ok(server) -> None:
    with urllib.request.urlopen(server.url("/health")) as response:
        payload = json.loads(response.read())
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["serial"] == "2601255637"
    assert payload["age_seconds"] is not None


def test_health_reports_failure_with_503(server) -> None:
    server.collector._reader.fail = True  # noqa: SLF001 - test double
    server.collector.poll_once()
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(server.url("/health"))
    assert error.value.code == 503
    payload = json.loads(error.value.read())
    assert payload["ok"] is False
    assert payload["error"]


def test_unknown_path_returns_404(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(server.url("/nope"))
    assert error.value.code == 404


def test_metrics_survive_failed_scrape(server) -> None:
    server.collector._reader.fail = True  # noqa: SLF001 - test double
    server.collector.poll_once()
    with urllib.request.urlopen(server.url("/metrics")) as response:
        body = response.read().decode()
    assert "deye_scrape_success 0" in body
