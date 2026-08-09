from bafang_can.constants import CanOperation, DeviceId
from bafang_can.frame import (
    BafangId,
    checksum,
    encode_id,
    int_to_bytes_le,
    string_from_bytes,
    string_to_bytes,
)


def test_id_roundtrip():
    ident = BafangId(
        source=DeviceId.TOOL,
        target=DeviceId.DRIVE_UNIT,
        operation=CanOperation.READ_CMD,
        code=0x60,
        subcode=0x11,
    )
    assert BafangId.decode(ident.encode()) == ident


def test_id_matches_reference_layout():
    # vendor/bafang_canable_pro builds [0x80|src, (tgt<<3)|op, code, sub] and
    # sends it as a big-endian 32 bit word with the EFF flag in bit 31.
    source, target, op, code, sub = 0x05, 0x02, 0x01, 0x60, 0x11
    reference = (
        ((0x80 | source) << 24) | (((target << 3) | op) << 16) | (code << 8) | sub
    )
    assert encode_id(source, target, op, code, sub) == reference & 0x1FFFFFFF


def test_checksum_is_low_byte_of_sum():
    assert checksum([0xFF, 0x01]) == 0x00
    assert checksum(range(10)) == 45


def test_little_endian_helpers():
    assert int_to_bytes_le(0x1234, 2) == [0x34, 0x12]


def test_strings_stop_at_terminator():
    assert string_from_bytes(b"HM\x00\xff\xff") == "HM"
    assert string_to_bytes("AB") == [0x41, 0x42, 0x00]
