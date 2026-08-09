import pytest

from bafang_can import codecs
from bafang_can.commands import READ, WRITE
from bafang_can.constants import CanOperation, DeviceId
from bafang_can.frame import BafangId
from bafang_can.protocol import BafangClient, DeviceError, TimeoutError_
from bafang_can.system import BafangSystem

from .fake_device import FakeBus, make_block


@pytest.fixture()
def system():
    bus = FakeBus(
        {
            (0x60, 0x00): b"HM1.0\x00",
            (0x60, 0x01): b"SW1.2\x00",
            (0x60, 0x11): make_block(0x11),
            (0x32, 0x03): bytes([0xC4, 0x09, 0xB5, 0x01, 0xA7, 0x08]),
            (0x60, 0x07): b"0821\x00",
        }
    )
    client = BafangClient(bus, timeout=1.0).start()
    try:
        yield bus, client, BafangSystem(client)
    finally:
        client.close()


def test_single_frame_read(system):
    _, client, _ = system
    message = client.read(DeviceId.DRIVE_UNIT, READ["HardwareVersion"])
    assert message.data.startswith(b"HM1.0")


def test_missing_command_raises_device_error(system):
    _, client, _ = system
    with pytest.raises(DeviceError):
        client.read(DeviceId.DRIVE_UNIT, READ["Parameter2"], retries=0)


def test_unreachable_device_times_out(system):
    _, client, _ = system
    with pytest.raises(TimeoutError_):
        client.read(DeviceId.BATTERY, READ["HardwareVersion"], timeout=0.2, retries=0)


def test_multiframe_read_is_reassembled_and_acknowledged(system):
    bus, client, _ = system
    message = client.read(DeviceId.DRIVE_UNIT, READ["Parameter1"])
    assert message.multiframe
    assert len(message.data) == 64
    assert message.data == make_block(0x11)

    acks = [
        BafangId.decode(m.arbitration_id)
        for m in bus.sent
        if BafangId.decode(m.arbitration_id).operation == CanOperation.NORMAL_ACK
    ]
    # start + 8 data frames + end
    assert len(acks) >= 9
    assert all(a.code == 0x60 and a.subcode == 0x11 for a in acks)


def test_multiframe_write_is_framed_correctly(system):
    bus, client, _ = system
    payload = make_block(0x22)
    client.write_long(DeviceId.DRIVE_UNIT, WRITE["Parameter1"], payload)

    ops = [BafangId.decode(m.arbitration_id).operation for m in bus.sent]
    assert ops[0] == CanOperation.WRITE_CMD
    assert bytes(bus.sent[0].data) == bytes([64])
    assert ops[1] == CanOperation.MULTIFRAME_START
    assert ops[-1] == CanOperation.MULTIFRAME_END
    assert bus.blocks[(0x60, 0x11)] == payload


def test_write_then_read_roundtrip_through_system(system):
    _, _, sys_ = system
    block = sys_.read_block("Parameter1")
    block.current_limit = 12
    sys_.write_block("Parameter1", block)
    after = sys_.read_block("Parameter1")
    assert after.current_limit == 12
    assert after.checksum_ok


def test_scan_reports_only_answering_devices(system):
    _, _, sys_ = system
    result = sys_.scan()
    assert result[DeviceId.DRIVE_UNIT] is True
    assert result[DeviceId.BATTERY] is False


def test_errors_are_decoded_with_descriptions(system):
    _, _, sys_ = system
    errors = sys_.errors()
    assert [code for code, _, _ in errors] == [8, 21]
    assert "hall" in errors[0][1].lower()


def test_dump_contains_raw_blocks(system):
    _, _, sys_ = system
    data = sys_.dump()
    assert data["drive_unit"]["Parameter1"]["raw"] == make_block(0x11).hex()
    assert data["devices"]["DRIVE_UNIT"]["present"] is True


def test_apply_dry_run_writes_nothing(system):
    bus, _, sys_ = system
    data = sys_.dump()
    before = dict(bus.blocks)
    report = sys_.apply(data, dry_run=True)
    assert any("would write" in line for line in report)
    assert bus.blocks == before


def test_apply_refuses_corrupt_backup(system):
    _, _, sys_ = system
    data = sys_.dump()
    corrupt = bytearray(bytes.fromhex(data["drive_unit"]["Parameter1"]["raw"]))
    corrupt[63] ^= 0xFF
    data["drive_unit"]["Parameter1"]["raw"] = bytes(corrupt).hex()
    report = sys_.apply(data, blocks=["Parameter1"], dry_run=False)
    assert any("checksum invalid" in line for line in report)


def test_speed_parameters_read(system):
    _, _, sys_ = system
    params = sys_.read_speed_parameters()
    assert params.speed_limit == 25.0
    assert params.wheel.text == "27.5"
    assert params.circumference == 2215
    assert isinstance(params, codecs.SpeedParameters)
