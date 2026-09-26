"""NDTP wire codec: stream framing, CRC, and decoding of what the telematics terminals send.

Pure functions and dataclasses only — no sockets, no asyncio, no app state — so the codec is
unit-testable on raw bytes and reusable by the TCP server, replay tools and fake-device clients.

Wire format (all integers little-endian, packed). Verified against the emulator's own bytecode
(``ndtp-telemetry-emulator:1.0``, ``CellFactory.wrapNphPacket``), not just the spec::

    frame = NPL header (15 B) | NPH header (10 B) | body
    NPL   = signature u16 (0x7E7E) | data_size u16 (= len(NPH + body)) | flags u16
          | crc u16 | type u8 (2 = NPH) | peer_address u32 (= unitId) | request_id u16
    NPH   = service_id u16 | type u16 | flags u16 (bit0 = request) | request_id u32

CRC is CRC-16/MODBUS over NPH + body. The emulator byte-swaps it before writing it into the
little-endian field, so on the wire it reads as **big-endian**. The NPL ``crc`` flag bit is
always 0, yet the CRC is always filled in — so we always verify it.

Realtime bodies are a sequence of cells ``[type u8][number u8][payload]`` with **no length
prefix**: the payload size is implied by the type. A cell type we don't know the size of
therefore ends parsing of that packet (see ``Realtime.unparsed_tail``). ``G6CellNav00`` is
always first, so navigation is never lost to this.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ----------------------------------------------------------------------------- constants

SIGNATURE = 0x7E7E
SIGNATURE_BYTES = b"\x7e\x7e"
NPL_SIZE = 15
NPH_SIZE = 10
NPL_TYPE_NPH = 2

SERVICE_GENERIC_CONTROLS = 0
SERVICE_NAVDATA = 1
NPH_SGC_CONN_REQUEST = 100
NPH_SND_REALTIME = 101

CELL_NAV = 0
# Payload sizes (bytes, without the 2-byte cell header) of the cells documented in the spec.
# Sizes of the remaining types (door counters, RFID, ...) aren't documented.
CELL_PAYLOAD_SIZES: dict[int, int] = {
    0: 26,   # G6CellNav00        navigation
    2: 26,   # G6CellIntSensor02  internal sensors
    8: 6,    # G6CellUsi08        fuel level sensor
    10: 37,  # G6CellCan10        CAN bus
    15: 50,  # G6CellLls15        LLS fuel sensor
    16: 8,   # G6CellTermo16      temperature
}

# Emulator frames are ~40-200 B. A cap well below the u16 maximum turns a corrupted length
# field into a fast resync instead of a stall waiting for tens of KB that never arrive.
DEFAULT_MAX_DATA_SIZE = 4096

_NPL_PREFIX = struct.Struct("<HHH")    # signature, data_size, flags   (crc follows, big-endian)
_NPL_SUFFIX = struct.Struct("<BIH")    # type, peer_address, request_id
_NPH = struct.Struct("<HHHI")          # service_id, type, flags, request_id
_HANDSHAKE = struct.Struct("<HHHIII")  # proto hi, proto lo, flags, peer_address, max_packet_size, reserved
_NAV = struct.Struct("<IIIBBHHHHHBB")  # timestamp, lon, lat, flags, bat, spd_avg, spd_max, course, track, alt, nsat, pdop

assert _NPL_PREFIX.size + 2 + _NPL_SUFFIX.size == NPL_SIZE
assert _NPH.size == NPH_SIZE
assert _HANDSHAKE.size == 18
assert _NAV.size == CELL_PAYLOAD_SIZES[CELL_NAV]


class NdtpDecodeError(ValueError):
    """A CRC-valid frame whose content doesn't match its declared message type."""


# ----------------------------------------------------------------------------- CRC


def _make_crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _make_crc_table()


def crc16_modbus(data: bytes | bytearray | memoryview) -> int:
    """CRC-16/MODBUS: init 0xFFFF, reflected poly 0xA001, no final xor."""
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


# ----------------------------------------------------------------------------- headers and frames


@dataclass(frozen=True, slots=True)
class NplHeader:
    data_size: int
    flags: int
    crc: int
    type: int
    peer_address: int  # = unitId of the terminal
    request_id: int


@dataclass(frozen=True, slots=True)
class NphHeader:
    service_id: int
    type: int
    flags: int
    request_id: int

    @property
    def is_request(self) -> bool:
        return bool(self.flags & 1)


@dataclass(frozen=True, slots=True)
class Frame:
    """One CRC-verified NPL frame. ``payload`` is the NPH header + body."""

    npl: NplHeader
    payload: bytes


@dataclass(slots=True)
class DecoderStats:
    frames: int = 0
    bytes_discarded: int = 0   # garbage between frames, bytes skipped while resyncing
    crc_errors: int = 0
    bad_headers: int = 0       # impossible data_size
    unsupported_type: int = 0  # CRC-valid frames with an NPL type other than NPH


class FrameDecoder:
    """Incremental decoder for one TCP connection: ``feed()`` raw bytes, get complete frames.

    TCP is a byte stream, so a read may hold half a frame or several frames. Framing errors
    never raise: garbage is skipped, a bad frame is dropped and the decoder resyncs on the next
    signature, with everything counted in ``stats``.
    """

    def __init__(self, *, max_data_size: int = DEFAULT_MAX_DATA_SIZE) -> None:
        self.max_data_size = max_data_size
        self.stats = DecoderStats()
        self._buf = bytearray()

    def feed(self, data: bytes | bytearray | memoryview) -> list[Frame]:
        buf = self._buf
        buf += data
        frames: list[Frame] = []
        while True:
            start = buf.find(SIGNATURE_BYTES)
            if start < 0:
                # a trailing 0x7E may be the first half of a signature split across reads
                self._discard(len(buf) - (1 if buf[-1:] == b"\x7e" else 0))
                return frames
            if start:
                self._discard(start)
            if len(buf) < NPL_SIZE:
                return frames

            _sig, size, flags = _NPL_PREFIX.unpack_from(buf, 0)
            if not NPH_SIZE <= size <= self.max_data_size:
                self.stats.bad_headers += 1
                self._discard(1)
                continue
            end = NPL_SIZE + size
            if len(buf) < end:
                return frames

            crc = int.from_bytes(buf[6:8], "big")
            payload = bytes(buf[NPL_SIZE:end])
            if crc16_modbus(payload) != crc:
                self.stats.crc_errors += 1
                self._discard(1)
                continue

            npl_type, peer_address, request_id = _NPL_SUFFIX.unpack_from(buf, 8)
            del buf[:end]
            if npl_type != NPL_TYPE_NPH:
                self.stats.unsupported_type += 1
                continue
            self.stats.frames += 1
            frames.append(Frame(NplHeader(size, flags, crc, npl_type, peer_address, request_id), payload))

    def _discard(self, n: int) -> None:
        if n > 0:
            del self._buf[:n]
            self.stats.bytes_discarded += n


# ----------------------------------------------------------------------------- messages


@dataclass(frozen=True, slots=True)
class Handshake:
    """``NPH_SGC_CONN_REQUEST`` — the first packet on every (re)connection."""

    nph: NphHeader
    proto_version: tuple[int, int]
    flags: int
    peer_address: int
    max_packet_size: int


@dataclass(frozen=True, slots=True)
class NavCell:
    """``G6CellNav00``: one navigation fix."""

    number: int
    timestamp: int         # Unix seconds, UTC (the terminal's clock, not dataset time)
    latitude: float        # signed degrees, sign from the N/S bit
    longitude: float       # signed degrees, sign from the E/W bit
    location_valid: bool   # extraDopBit7
    flags: int             # raw extraDopBit0..7 byte, bit0 = LSB
    bat_voltage_mv: int
    speed_avg_kmh: int
    speed_max_kmh: int
    course_deg: int
    track_m: int
    altitude_m: int
    nsat: int
    pdop: int

    @property
    def alarm(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def sos(self) -> bool:
        return bool(self.flags & 0x04)


@dataclass(frozen=True, slots=True)
class RawCell:
    """A cell of known size whose fields we don't decode."""

    type: int
    number: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class Realtime:
    """``NPH_SND_REALTIME`` — a telemetry packet."""

    nph: NphHeader
    nav: NavCell | None
    cells: tuple[NavCell | RawCell, ...]
    unparsed_tail: bytes = field(default=b"")  # from the first cell of unknown size onward


@dataclass(frozen=True, slots=True)
class UnknownMessage:
    nph: NphHeader
    body: bytes


Message = Handshake | Realtime | UnknownMessage


def decode_frame(frame: Frame) -> Message:
    """Interpret a frame's NPH header and body. Raises ``NdtpDecodeError`` on malformed content."""
    if len(frame.payload) < NPH_SIZE:
        raise NdtpDecodeError(f"payload of {len(frame.payload)} B is shorter than the NPH header")
    nph = NphHeader(*_NPH.unpack_from(frame.payload, 0))
    body = frame.payload[NPH_SIZE:]
    kind = (nph.service_id, nph.type)

    if kind == (SERVICE_GENERIC_CONTROLS, NPH_SGC_CONN_REQUEST):
        if len(body) < _HANDSHAKE.size:
            raise NdtpDecodeError(f"handshake body of {len(body)} B, expected {_HANDSHAKE.size}")
        hi, lo, flags, peer_address, max_packet_size, _reserved = _HANDSHAKE.unpack_from(body, 0)
        return Handshake(nph, (hi, lo), flags, peer_address, max_packet_size)

    if kind == (SERVICE_NAVDATA, NPH_SND_REALTIME):
        cells, tail = _decode_cells(body)
        nav = next((c for c in cells if isinstance(c, NavCell)), None)
        return Realtime(nph, nav, cells, tail)

    return UnknownMessage(nph, body)


def _decode_cells(body: bytes) -> tuple[tuple[NavCell | RawCell, ...], bytes]:
    cells: list[NavCell | RawCell] = []
    off = 0
    while off < len(body):
        size = CELL_PAYLOAD_SIZES.get(body[off])
        if size is None or off + 2 + size > len(body):
            return tuple(cells), body[off:]
        cell_type, number = body[off], body[off + 1]
        payload = body[off + 2 : off + 2 + size]
        cells.append(_decode_nav(number, payload) if cell_type == CELL_NAV else RawCell(cell_type, number, payload))
        off += 2 + size
    return tuple(cells), b""


def _decode_nav(number: int, payload: bytes) -> NavCell:
    ts, lon, lat, flags, bat, spd_avg, spd_max, course, track, alt, nsat, pdop = _NAV.unpack(payload)
    return NavCell(
        number=number,
        timestamp=ts,
        latitude=(lat if flags & 0x20 else -lat) / 1e7,
        longitude=(lon if flags & 0x40 else -lon) / 1e7,
        location_valid=bool(flags & 0x80),
        flags=flags,
        bat_voltage_mv=bat * 20,
        speed_avg_kmh=spd_avg,
        speed_max_kmh=spd_max,
        course_deg=course,
        track_m=track,
        altitude_m=alt,
        nsat=nsat,
        pdop=pdop,
    )


# ----------------------------------------------------------------------------- encoding
# Mirrors the emulator byte for byte. Used by tests and fake-device clients.


def encode_frame(service_id: int, nph_type: int, body: bytes, *, peer_address: int,
                 nph_request_id: int = 0, request: bool = True) -> bytes:
    payload = _NPH.pack(service_id, nph_type, int(request), nph_request_id) + body
    return (
        _NPL_PREFIX.pack(SIGNATURE, len(payload), 0)
        + crc16_modbus(payload).to_bytes(2, "big")
        + _NPL_SUFFIX.pack(NPL_TYPE_NPH, peer_address, 0)
        + payload
    )


def encode_handshake(unit_id: int, *, request_id: int = 1, max_packet_size: int = 65535) -> bytes:
    body = _HANDSHAKE.pack(6, 2, 0, unit_id, max_packet_size, 0)
    return encode_frame(SERVICE_GENERIC_CONTROLS, NPH_SGC_CONN_REQUEST, body,
                        peer_address=unit_id, nph_request_id=request_id)


def encode_nav_cell(*, timestamp: int, latitude: float, longitude: float, valid: bool = True,
                    speed_kmh: int = 0, course_deg: int = 0, altitude_m: int = 0, number: int = 0) -> bytes:
    """A complete ``G6CellNav00`` cell (2-byte cell header + 26-byte payload)."""
    flags = (0x20 if latitude >= 0 else 0) | (0x40 if longitude >= 0 else 0) | (0x80 if valid else 0)
    payload = _NAV.pack(timestamp, round(abs(longitude) * 1e7), round(abs(latitude) * 1e7), flags,
                        0, speed_kmh, speed_kmh, course_deg, 0, altitude_m, 0, 0)
    return bytes((CELL_NAV, number)) + payload


def encode_realtime(unit_id: int, cells: bytes, *, request_id: int) -> bytes:
    return encode_frame(SERVICE_NAVDATA, NPH_SND_REALTIME, cells, peer_address=unit_id, nph_request_id=request_id)
