"""P0: paper-tape simulator for the MC68000 self-host project.

Mimics the tape contract decided on 2026-11-04/05:
  - 16 slots x 1MB, continuous numbering (PSRAM 0-7, FLASH 8-15)
  - per-slot read pointer (RP) and write pointer (WP = byte length)
  - streaming access only: select / new / rewind / read byte / write byte
  - EOF is RP >= WP; the firmware will also expose WP via LEN registers,
    here that is TapeDeck.length()
  - the tape itself holds raw bytes only: no terminator, no header,
    no checksum inside the tape (all of that lives in the transfer format)

Deliberate difference from the planned firmware: read_byte() at EOF
returns None instead of a dummy byte, so prototype bugs (reading past
the end) surface immediately instead of silently consuming zeros.

Transfer helpers (PC side of the decided formats):
  - bsd_sum(): the old Unix/BSD sum Kyo used on EMUZ80
    (rotate the 16-bit sum right by 1, add the byte, mask to 16 bits)
  - uuencode()/uudecode(): classic format, ENC maps 0 to backtick,
    same as usr/src/cmd/uuencode.c in EMUZ80_RP2040_PCB_Firmware
"""

from __future__ import annotations

NUM_SLOTS = 16
TAPE_SIZE = 1024 * 1024  # 1MB per slot, decided 2026-11-04
PSRAM_SLOTS = range(0, 8)
FLASH_SLOTS = range(8, 16)


class TapeError(Exception):
    pass


class TapeDeck:
    def __init__(self) -> None:
        self._buf = [bytearray(TAPE_SIZE) for _ in range(NUM_SLOTS)]
        self._wp = [0] * NUM_SLOTS   # write pointer == recorded byte length
        self._rp = [0] * NUM_SLOTS   # read pointer, kept per slot
        self._cur = 0

    # ---- slot selection -------------------------------------------------
    def select(self, slot: int) -> None:
        if not 0 <= slot < NUM_SLOTS:
            raise TapeError(f"slot out of range: {slot}")
        self._cur = slot

    @property
    def selected(self) -> int:
        return self._cur

    # ---- streaming contract (the only API passes may use) ---------------
    def new_tape(self) -> None:
        """CMD 'new tape': WP=0, RP=0 on the selected slot."""
        self._wp[self._cur] = 0
        self._rp[self._cur] = 0

    def rewind(self) -> None:
        """CMD 'rewind read': RP=0 on the selected slot."""
        self._rp[self._cur] = 0

    def write_byte(self, b: int) -> None:
        if not 0 <= b <= 255:
            raise TapeError(f"not a byte: {b}")
        wp = self._wp[self._cur]
        if wp >= TAPE_SIZE:
            raise TapeError(f"tape full: slot {self._cur}")
        self._buf[self._cur][wp] = b
        self._wp[self._cur] = wp + 1

    def read_byte(self) -> int | None:
        """Next byte and RP advances; None at EOF (RP >= WP)."""
        rp = self._rp[self._cur]
        if rp >= self._wp[self._cur]:
            return None
        self._rp[self._cur] = rp + 1
        return self._buf[self._cur][rp]

    def eof(self) -> bool:
        return self._rp[self._cur] >= self._wp[self._cur]

    def length(self) -> int:
        """Byte length (WP) of the selected slot; what LEN registers expose."""
        return self._wp[self._cur]

    # ---- PC-side convenience (driver/tests only, NOT for passes) --------
    def length_of(self, slot: int) -> int:
        return self._wp[slot]

    def load_bytes(self, slot: int, data: bytes) -> None:
        if len(data) > TAPE_SIZE:
            raise TapeError("data larger than one tape")
        self.select(slot)
        self.new_tape()
        self._buf[slot][: len(data)] = data
        self._wp[slot] = len(data)

    def dump_bytes(self, slot: int) -> bytes:
        return bytes(self._buf[slot][: self._wp[slot]])

    def load_file(self, slot: int, path: str, *, text: bool = False) -> int:
        with open(path, "rb") as f:
            data = f.read()
        if text:
            data = normalize_text(data)
        self.load_bytes(slot, data)
        return len(data)

    def save_file(self, slot: int, path: str) -> int:
        data = self.dump_bytes(slot)
        with open(path, "wb") as f:
            f.write(data)
        return len(data)


def normalize_text(data: bytes) -> bytes:
    """Canonical text form decided 2026-11-05: LF line endings, and a
    CP/M ^Z (and anything after it) is treated as end of text."""
    cut = data.find(b"\x1a")
    if cut >= 0:
        data = data[:cut]
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


# ---- BSD sum (old Unix `sum`, as in Kyo's EMUZ80 port) -------------------

def bsd_sum(data: bytes) -> int:
    s = 0
    for c in data:
        if s & 1:
            s = (s >> 1) + 0x8000
        else:
            s >>= 1
        s = (s + c) & 0xFFFF
    return s


def format_sum(data: bytes) -> str:
    """The %05u field of the classic sum output."""
    return "%05d" % bsd_sum(data)


# ---- uuencode / uudecode --------------------------------------------------

def _enc(c: int) -> int:
    """ENC() from uuencode.c: 0 becomes backtick, else (c & 077) + ' '."""
    return 0x60 if c == 0 else (c & 0o77) + 0x20


def _dec(c: int) -> int:
    return (c - 0x20) & 0o77


def uuencode(data: bytes, name: str, mode: int = 0o644) -> bytes:
    out = bytearray()
    out += ("begin %o %s\n" % (mode, name)).encode("ascii")
    for off in range(0, len(data), 45):
        chunk = data[off : off + 45]
        n = len(chunk)
        out.append(_enc(n))
        padded = chunk + b"\x00" * ((3 - n % 3) % 3)
        for i in range(0, len(padded), 3):
            b0, b1, b2 = padded[i], padded[i + 1], padded[i + 2]
            out.append(_enc(b0 >> 2))
            out.append(_enc(((b0 << 4) & 0x30) | (b1 >> 4)))
            out.append(_enc(((b1 << 2) & 0x3C) | (b2 >> 6)))
            out.append(_enc(b2 & 0x3F))
        out.append(0x0A)
    out += b"`\nend\n"
    return bytes(out)


def uudecode(data: bytes) -> tuple[str, int, bytes]:
    lines = data.split(b"\n")
    name, mode = "", 0o644
    body = bytearray()
    started = False
    for line in lines:
        if not started:
            if line.startswith(b"begin "):
                parts = line.split(b" ", 2)
                mode = int(parts[1], 8)
                name = parts[2].decode("ascii", "replace")
                started = True
            continue
        if line == b"end":
            break
        if not line:
            continue
        n = _dec(line[0])
        if n == 0:
            continue
        chars = line[1:]
        decoded = bytearray()
        for i in range(0, len(chars), 4):
            g = chars[i : i + 4]
            if len(g) < 4:
                g = g + b"`" * (4 - len(g))
            v = [_dec(c) for c in g]
            decoded.append(((v[0] << 2) | (v[1] >> 4)) & 0xFF)
            decoded.append(((v[1] << 4) | (v[2] >> 2)) & 0xFF)
            decoded.append(((v[2] << 6) | v[3]) & 0xFF)
        body += decoded[:n]
    if not started:
        raise TapeError("uudecode: no begin line")
    return name, mode, bytes(body)
