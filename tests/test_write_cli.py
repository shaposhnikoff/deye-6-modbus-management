"""Tests for the dmon-write CLI (dry-run by default, --confirm to write)."""

from __future__ import annotations

import json

import pytest

from deye_monitoring import write_cli

TOU_BLOCK = {
    248: 255,
    250: 100,
    251: 500,
    252: 900,
    253: 1300,
    254: 1700,
    255: 2100,
    256: 5000,
    257: 6000,
    258: 6000,
    259: 6000,
    260: 6000,
    261: 6000,
    262: 4900,
    263: 4900,
    264: 4900,
    265: 4900,
    266: 4900,
    267: 4900,
    268: 85,
    269: 85,
    270: 85,
    271: 85,
    272: 85,
    273: 85,
    274: 5,
    275: 5,
    276: 7,
    277: 7,
    278: 7,
    279: 7,
}


class FakeClient:
    """Records writes so the tests can prove nothing was sent."""

    instances: list[FakeClient] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.registers = dict(TOU_BLOCK)
        self.writes: list[tuple[int, int]] = []
        FakeClient.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read_holding_registers(self, start: int, count: int) -> list[int]:
        return [self.registers.get(start + index, 0) for index in range(count)]

    def write_register(self, address: int, value: int) -> None:
        self.writes.append((address, value))
        self.registers[address] = value


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.instances = []
    monkeypatch.setattr(write_cli, "WriteClient", FakeClient)
    return FakeClient


def _last() -> FakeClient:
    return FakeClient.instances[-1]


def test_parse_assignments() -> None:
    assert write_cli.parse_assignments(["tou.1.power=5500", "tou.2.start=2100"]) == {
        "tou.1.power": 5500.0,
        "tou.2.start": 2100.0,
    }


def test_parse_assignments_rejects_garbage() -> None:
    with pytest.raises(write_cli.WriteValueError):
        write_cli.parse_assignments(["tou.1.power"])
    with pytest.raises(write_cli.WriteValueError):
        write_cli.parse_assignments(["tou.1.power=abc"])


def test_list_prints_targets(fake_client: type[FakeClient], capsys: pytest.CaptureFixture) -> None:
    assert write_cli.main(["list", "--port", "fake"]) == 0
    out = capsys.readouterr().out
    assert "tou.1.start" in out
    assert "01:00" in out
    assert "tou.1.grid_charge" in out
    assert "unverified" not in out  # mask targets stay hidden


def test_list_can_show_unverified(
    fake_client: type[FakeClient], capsys: pytest.CaptureFixture
) -> None:
    assert write_cli.main(["list", "--port", "fake", "--include-unverified"]) == 0
    assert "charge_mask" in capsys.readouterr().out


def test_plan_writes_nothing(fake_client: type[FakeClient], capsys: pytest.CaptureFixture) -> None:
    assert write_cli.main(["plan", "--port", "fake", "tou.1.power=5500"]) == 0
    assert _last().writes == []
    out = capsys.readouterr().out
    assert "5000 W -> 5500 W" in out
    assert "dry run" in out


def test_set_without_confirm_writes_nothing(
    fake_client: type[FakeClient], capsys: pytest.CaptureFixture
) -> None:
    assert write_cli.main(["set", "--port", "fake", "tou.1.start=2100"]) == 0
    assert _last().writes == []
    assert "re-run with --confirm" in capsys.readouterr().out


def test_set_with_confirm_writes_and_audits(
    fake_client: type[FakeClient], tmp_path, capsys: pytest.CaptureFixture
) -> None:
    log = tmp_path / "writes.log"
    assert (
        write_cli.main(
            ["set", "--port", "fake", "--confirm", "--log", str(log), "tou.1.power=5500"]
        )
        == 0
    )
    assert _last().writes == [(256, 5500)]
    record = json.loads(log.read_text().splitlines()[0])
    assert record["key"] == "tou.1.power"
    assert record["confirmed"] is True
    assert "verified by readback" in capsys.readouterr().out


def test_set_bit_target_preserves_gm_bit(fake_client: type[FakeClient], tmp_path) -> None:
    log = tmp_path / "writes.log"
    assert (
        write_cli.main(
            ["set", "--port", "fake", "--confirm", "--log", str(log), "tou.1.grid_charge=0"]
        )
        == 0
    )
    # 5 = grid+GM -> clearing grid keeps GM: 4
    assert _last().writes == [(274, 4)]


def test_unknown_key_exits_2(fake_client: type[FakeClient]) -> None:
    assert write_cli.main(["plan", "--port", "fake", "tou.1.nope=1"]) == 2


def test_out_of_range_exits_2(fake_client: type[FakeClient]) -> None:
    assert write_cli.main(["plan", "--port", "fake", "tou.1.power=99999"]) == 2


def test_verification_failure_exits_3(
    fake_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def stubborn(client: FakeClient, change: object) -> None:
        raise write_cli.WriteVerificationError("readback mismatch")

    monkeypatch.setattr(write_cli, "apply_change", stubborn)
    log = tmp_path / "writes.log"
    assert (
        write_cli.main(
            ["set", "--port", "fake", "--confirm", "--log", str(log), "tou.1.power=5500"]
        )
        == 3
    )
    record = json.loads(log.read_text().splitlines()[0])
    assert record["confirmed"] is False


def test_verify_reports_match(fake_client: type[FakeClient], capsys: pytest.CaptureFixture) -> None:
    assert write_cli.main(["verify", "--port", "fake", "tou.1.power=5000", "tou.1.start=100"]) == 0
    out = capsys.readouterr().out
    assert "MATCH" in out
    assert "2/2 match" in out


def test_verify_reports_drift_and_exits_1(
    fake_client: type[FakeClient], capsys: pytest.CaptureFixture
) -> None:
    assert write_cli.main(["verify", "--port", "fake", "tou.1.power=9999"]) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert "0/1 match" in out


def test_verify_checks_bits(fake_client: type[FakeClient]) -> None:
    # register 274 reads 5 = grid on, GM on
    assert write_cli.main(["verify", "--port", "fake", "tou.1.grid_charge=1"]) == 0
    assert write_cli.main(["verify", "--port", "fake", "tou.1.gen_charge=1"]) == 1


def test_verify_never_writes(fake_client: type[FakeClient]) -> None:
    write_cli.main(["verify", "--port", "fake", "tou.1.power=9999"])
    assert _last().writes == []


def test_verify_unknown_key_exits_2(fake_client: type[FakeClient]) -> None:
    assert write_cli.main(["verify", "--port", "fake", "tou.1.nope=1"]) == 2
