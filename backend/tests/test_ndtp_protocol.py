"""NDTP codec tests.

``fixtures/emulator_*.bin`` are raw TCP streams captured from ``ndtp-telemetry-emulator:1.0``
(one handshake + five realtime packets each, intervalMs=1000), configured as:

* ``explicit_moscow``      unitId 664030, explicit Nav00: 557551234 / 376173210, N/E, valid,
                           speedAvg 45, speedMax 52, course 225, altitude 150, nsat 11, pdop 3
* ``south_west_invalid``   unitId 794446, explicit Nav00: 339000000 / 704000000, S/W, invalid, course 90
* ``autogenerate``         unitId 1166336, autoGenerate with default cells (Nav00, Usi08, Termo16,
                           IntSensor02, Can10)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ndtp.protocol import (
    FrameDecoder,
    Handshake,
    NavCell,
    RawCell,
    Realtime,
    UnknownMessage,
    crc16_modbus,
    decode_frame,
    encode_frame,
    encode_handshake,
    encode_nav_cell,
    encode_realtime,
)

FIXTURES = Path(__file__).parent / "fixtures"


def decode_stream(raw: bytes) -> tuple[list, FrameDecoder]:
    dec = FrameDecoder()
    return [decode_frame(f) for f in dec.feed(raw)], dec


def fixture(name: str) -> bytes:
    return (FIXTURES / f"emulator_{name}.bin").read_bytes()


# ----------------------------------------------------------------------------- CRC


def test_crc16_modbus_check_value() -> None:
    assert crc16_modbus(b"123456789") == 0x4B37  # catalogue check value of CRC-16/MODBUS


# ----------------------------------------------------------------------------- real emulator bytes


def test_emulator_explicit_fix() -> None:
    msgs, dec = decode_stream(fixture("explicit_moscow"))
    assert dec.stats.frames == 6 and dec.stats.crc_errors == 0 and dec.stats.bytes_discarded == 0

    hs = msgs[0]
    assert isinstance(hs, Handshake)
    assert (hs.peer_address, hs.proto_version, hs.max_packet_size) == (664030, (6, 2), 65535)

    rts = msgs[1:]
    assert all(isinstance(m, Realtime) for m in rts)
    assert [m.nph.request_id for m in rts] == [2, 3, 4, 5, 6]
    nav = rts[0].nav
    assert nav.latitude == pytest.approx(55.7551234, abs=1e-9)
    assert nav.longitude == pytest.approx(37.6173210, abs=1e-9)
    assert nav.location_valid
    assert (nav.speed_avg_kmh, nav.speed_max_kmh, nav.course_deg, nav.altitude_m) == (45, 52, 225, 150)
    assert (nav.nsat, nav.pdop) == (11, 3)
    assert [m.nav.timestamp for m in rts] == sorted(m.nav.timestamp for m in rts)


def test_emulator_sign_bits_and_invalid_fix() -> None:
    msgs, _ = decode_stream(fixture("south_west_invalid"))
    nav = msgs[1].nav
    assert nav.latitude == pytest.approx(-33.9)
    assert nav.longitude == pytest.approx(-70.4)
    assert not nav.location_valid


def test_emulator_autogenerate_walks_every_cell() -> None:
    msgs, _ = decode_stream(fixture("autogenerate"))
    rt = msgs[1]
    assert [type(c).__name__ for c in rt.cells] == ["NavCell", "RawCell", "RawCell", "RawCell", "RawCell"]
    assert [c.type for c in rt.cells[1:]] == [8, 16, 2, 10]  # Usi08, Termo16, IntSensor02, Can10
    assert rt.unparsed_tail == b""
    assert 55.0 < rt.nav.latitude < 56.5 and 37.0 < rt.nav.longitude < 38.5  # random walk near Moscow


def test_encoder_reproduces_emulator_handshake_byte_for_byte() -> None:
    raw = fixture("explicit_moscow")
    assert encode_handshake(664030) == raw[:43]  # NPL 15 + NPH 10 + handshake 18


@pytest.mark.parametrize("name", ["explicit_moscow", "south_west_invalid", "autogenerate"])
def test_emulator_stream_fed_byte_by_byte(name: str) -> None:
    raw = fixture(name)
    whole, _ = decode_stream(raw)
    dec = FrameDecoder()
    trickled = [decode_frame(f) for i in range(len(raw)) for f in dec.feed(raw[i : i + 1])]
    assert trickled == whole


# ----------------------------------------------------------------------------- framing robustness


def _realtime(unit_id: int = 42, request_id: int = 2, lat: float = 55.75, lon: float = 37.61) -> bytes:
    return encode_realtime(unit_id, encode_nav_cell(timestamp=1_767_670_500, latitude=lat, longitude=lon,
                                                    speed_kmh=30, course_deg=90), request_id=request_id)


def test_round_trip() -> None:
    (msg,), _ = decode_stream(_realtime(lat=-12.5, lon=-45.25))
    assert isinstance(msg, Realtime)
    assert (msg.nav.latitude, msg.nav.longitude, msg.nav.timestamp) == (-12.5, -45.25, 1_767_670_500)


def test_garbage_before_frame_is_skipped() -> None:
    msgs, dec = decode_stream(b"\x00\xffjunk\x7e" + _realtime())
    assert len(msgs) == 1 and dec.stats.bytes_discarded == 7


def test_corrupt_crc_drops_frame_and_next_frame_survives() -> None:
    bad = bytearray(_realtime(request_id=2))
    bad[-1] ^= 0xFF
    msgs, dec = decode_stream(bytes(bad) + _realtime(request_id=3))
    assert [m.nph.request_id for m in msgs] == [3]
    assert dec.stats.crc_errors == 1


def test_signature_split_across_reads() -> None:
    raw = _realtime()
    dec = FrameDecoder()
    assert dec.feed(raw[:1]) == []
    assert len(dec.feed(raw[1:])) == 1
    assert dec.stats.bytes_discarded == 0


def test_impossible_data_size_resyncs() -> None:
    bogus = b"\x7e\x7e" + (3).to_bytes(2, "little") + b"\x00" * 11  # data_size 3 < NPH header
    msgs, dec = decode_stream(bogus + _realtime())
    assert len(msgs) == 1 and dec.stats.bad_headers == 1


def test_non_nph_npl_type_is_skipped_whole() -> None:
    frame = bytearray(_realtime(request_id=2))
    frame[8] = 3  # NPL type DEBUG; the CRC covers only NPH + body, so it stays valid
    msgs, dec = decode_stream(bytes(frame) + _realtime(request_id=3))
    assert [m.nph.request_id for m in msgs] == [3]
    assert dec.stats.unsupported_type == 1 and dec.stats.bytes_discarded == 0


def test_unknown_cell_type_stops_walk_but_keeps_nav() -> None:
    nav = encode_nav_cell(timestamp=1, latitude=55.0, longitude=37.0)
    door_cell = bytes((4, 0)) + b"\x01" * 20  # G6CellIrma04: size undocumented
    (msg,), _ = decode_stream(encode_realtime(7, nav + door_cell, request_id=2))
    assert isinstance(msg.nav, NavCell) and len(msg.cells) == 1
    assert msg.unparsed_tail == door_cell


def test_unknown_nph_service_is_passed_through() -> None:
    (msg,), _ = decode_stream(encode_frame(9, 9, b"\x01\x02", peer_address=1))
    assert isinstance(msg, UnknownMessage) and msg.body == b"\x01\x02"


def test_raw_cell_payload_is_sliced_exactly() -> None:
    termo = bytes((16, 0)) + (0).to_bytes(4, "little") + (-7).to_bytes(4, "little", signed=True)
    nav = encode_nav_cell(timestamp=1, latitude=1.0, longitude=1.0)
    (msg,), _ = decode_stream(encode_realtime(7, nav + termo, request_id=2))
    assert msg.cells[1] == RawCell(type=16, number=0, payload=termo[2:])
