import pytest

from bafang_can import codecs
from bafang_can.commands import READ, WRITE
from bafang_can.constants import CanOperation, DeviceId
from bafang_can.frame import BafangId, BafangMessage
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


def test_the_besst_command_table_is_covered():
    """Codes Bafang's own software uses that neither vendored project knows.

    ``OpenSourceEBike/Bafang_M500_M600`` extracted these straight out of the
    BESST desktop application's JavaScript. No payload layout is claimed for
    any of them -- they are here so that ``probe`` asks the bike about them and
    reports whether it answers, refuses or ignores each one, which is the only
    way this project finds out.
    """
    from bafang_can.commands import READ

    expected = {
        "SystemParams": (0x60, 0x06),
        "Parameter6013": (0x60, 0x13),
        "Parameter6014": (0x60, 0x14),
        "Parameter6015": (0x60, 0x15),
        "Parameter6016": (0x60, 0x16),
        "SensorCalibrationData": (0x61, 0x00),
        "ControllerFeatures": (0x62, 0x15),
        "ElectronicLock": (0x37, 0x00),
        "TransmissionInfo0": (0x36, 0x00),
        "BatteryCharacteristics": (0x64, 0x15),
        "CellsVoltage7": (0x64, 0x09),
    }
    for name, (code, subcode) in expected.items():
        assert (READ[name].code, READ[name].subcode) == (code, subcode), name


def test_no_two_commands_claim_the_same_code_for_the_same_device():
    """Growing the table from a third source is where a collision would appear."""
    from collections import defaultdict

    from bafang_can.commands import READ

    seen: dict[tuple, list[str]] = defaultdict(list)
    for name, command in READ.items():
        for device in command.devices:
            seen[(device, command.code, command.subcode)].append(name)

    clashes = {key: names for key, names in seen.items() if len(names) > 1}
    assert not clashes, clashes


def test_a_silent_command_is_retried_before_being_called_absent():
    """One lost frame must not become the claim "this firmware lacks it".

    Probing a real DP C340.C with a single attempt called its ErrorCode and
    SoftwareVersion silent; both answer on a retry. Silence is the reading the
    lock question rests on, so it has to survive more than one attempt.
    """
    attempts = []

    class FlakyClient:
        def read(self, device, command, timeout=None, retries=None):
            attempts.append(command.name)
            raise TimeoutError_("nothing came back")

    system = BafangSystem.__new__(BafangSystem)
    system.client = FlakyClient()

    assert system._outcome(DeviceId.DRIVE_UNIT, READ["Parameter1"], timeout=0.1) == "silent"
    assert len(attempts) == BafangSystem.PROBE_ATTEMPTS


def test_a_refusal_is_not_retried():
    """ERROR_ACK is positive evidence and arrives first time; retrying it only
    costs a real bike time on every probe."""
    attempts = []

    class RefusingClient:
        def read(self, device, command, timeout=None, retries=None):
            attempts.append(command.name)
            raise DeviceError("declined")

    system = BafangSystem.__new__(BafangSystem)
    system.client = RefusingClient()

    assert system._outcome(DeviceId.DRIVE_UNIT, READ["Parameter1"], timeout=0.1) == "refused"
    assert len(attempts) == 1


def _client_for_delivery():
    client = BafangClient.__new__(BafangClient)
    client.source = DeviceId.TOOL
    client._pending = {}
    client._lock = __import__("threading").Lock()
    client._listeners = []
    return client


def test_a_broadcast_does_not_satisfy_a_pending_write():
    """The drive unit broadcasts 32/03 every 2 s, and 32/03 is where a speed
    parameter write goes. Matching on source, code and subcode alone let that
    broadcast be reported as the acknowledgement of the write.

    A write this firmware ignored would then have been reported as successful
    whenever a broadcast happened to land inside the timeout -- on the one
    field `wheel` writes.
    """
    client = _client_for_delivery()
    pending = client._register((DeviceId.DRIVE_UNIT, 0x32, 0x03))

    broadcast = BafangMessage(
        id=BafangId(
            source=DeviceId.DRIVE_UNIT,
            target=DeviceId.BROADCAST,
            operation=CanOperation.WRITE_CMD,
            code=0x32,
            subcode=0x03,
        ),
        data=bytes.fromhex("c409c0019d08"),
    )
    client._deliver(broadcast)

    assert pending.queue.empty(), "a broadcast was accepted as an answer"


def test_a_real_acknowledgement_still_satisfies_the_request():
    """The fix must not deafen the client to genuine answers."""
    client = _client_for_delivery()
    pending = client._register((DeviceId.DRIVE_UNIT, 0x32, 0x03))

    ack = BafangMessage(
        id=BafangId(
            source=DeviceId.DRIVE_UNIT,
            target=DeviceId.TOOL,
            operation=CanOperation.NORMAL_ACK,
            code=0x32,
            subcode=0x03,
        ),
        data=b"\x00",
    )
    client._deliver(ack)

    assert pending.queue.get_nowait() is ack


def test_a_message_addressed_to_another_node_is_not_an_answer():
    """The display polls the battery for 64/00 constantly. Those are addressed
    to the battery and must never complete this tool's own request for it."""
    client = _client_for_delivery()
    pending = client._register((DeviceId.DISPLAY, 0x64, 0x00))

    client._deliver(
        BafangMessage(
            id=BafangId(
                source=DeviceId.DISPLAY,
                target=DeviceId.BATTERY,
                operation=CanOperation.READ_CMD,
                code=0x64,
                subcode=0x00,
            ),
            data=b"",
        )
    )

    assert pending.queue.empty()
