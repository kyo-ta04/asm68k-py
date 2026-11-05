"""P3: Pass 1 + Pass 2 prototype -- records -> 68000 machine code.

Reads the intermediate tape image (out/intermediate.bin, produced by
pass05.py) plus symbols.txt / opcodes.txt, then:

  pass 1: location-counter sweep. Labels get addresses, EQU chains
          are resolved by fixpoint (before the sweep for EQU-only
          chains, after it for label-dependent ones), and the two
          size decisions a real 2-pass assembler must freeze in
          pass 1 are recorded per record: absolute-address width
          (W/L) and unsuffixed branch size (S/W).
  pass 2: byte emission using the frozen decisions.

The image is compared byte-for-byte with the golden dump
(research/EhBASIC68k-EMU/EhBASICv354.txt, the exact 16KB ROM image).
"""

from __future__ import annotations

import os
import re
import struct

import tapedeck as td
from pass05 import (
    T_NONE, T_REGD, T_REGA, T_IND, T_POSTINC, T_PREDEC, T_DISP, T_INDEX,
    T_DISPPC, T_INDEXPC, T_ADDR, T_IMM, T_REGLIST, T_SPECIAL, T_STR,
    T_ITEM, K_NONE, K_CONST, K_SYM, K_CURPC, CONT,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")


# ---- input parsing ---------------------------------------------------------

def load_tables():
    symbols = []          # index = id-1 -> name
    kinds = {}
    with open(os.path.join(OUT, "symbols.txt")) as f:
        for line in f:
            sid, name, kind = line.split()
            symbols.append(name)
            kinds[int(sid)] = kind
    opcodes = []
    with open(os.path.join(OUT, "opcodes.txt")) as f:
        for line in f:
            _oid, name = line.split()
            opcodes.append(name)
    return symbols, kinds, opcodes


def parse_op(rec, base):
    return {
        "type": rec[base], "reg": rec[base + 1],
        "k1": rec[base + 2], "v1": struct.unpack_from("<I", rec, base + 3)[0],
        "op": rec[base + 7],
        "k2": rec[base + 8], "v2": struct.unpack_from("<I", rec, base + 9)[0],
    }


def parse_records(raw: bytes, opcodes):
    mains = []
    i = 0
    n = len(raw) // 32
    while i < n:
        rec = raw[i * 32:(i + 1) * 32]
        m = {
            "idx": len(mains),
            "label": struct.unpack_from("<H", rec, 0)[0],
            "opcode": None if rec[2] == 0 else opcodes[rec[2] - 1],
            "suffix": rec[3],
            "op1": parse_op(rec, 5),
            "op2": parse_op(rec, 18),
            "aux": rec[31],
            "flags": rec[4],
            "conts": [],
            "line": len(mains) + 1,   # formatted line number (1-based)
        }
        ext = rec[4] & 1
        i += 1
        while ext and i < n:
            c = raw[i * 32:(i + 1) * 32]
            kind = c[3]
            if kind == 1:
                m["conts"].append({"kind": "ITEM", "op": parse_op(c, 5)})
            elif kind == 2:
                m["conts"].append({"kind": "STR", "data": bytes(c[7:7 + c[31]])})
            elif kind == 3:
                steps = []
                for s in range(c[6]):
                    steps.append((c[7 + s * 5],
                                  struct.unpack_from("<I", c, 8 + s * 5)[0]))
                m["conts"].append({"kind": "EXPR", "which": c[5], "steps": steps})
            ext = c[4] & 1
            i += 1
        mains.append(m)
    return mains


# ---- expression evaluation ---------------------------------------------------

class Eval:
    def __init__(self, values):
        self.values = values    # id -> int | None

    def term(self, kind, val, pc):
        if kind == K_CONST:
            return val
        if kind == K_SYM:
            return self.values[val]
        if kind == K_CURPC:
            return pc
        return 0

    def fields(self, op, pc):
        """Evaluate an op's (term)+-(term) fields; None if unresolved."""
        a = self.term(op["k1"], op["v1"], pc)
        if a is None:
            return None
        if op["op"] == 0:
            return a
        b = self.term(op["k2"], op["v2"], pc)
        if b is None:
            return None
        return a + b if op["op"] == 1 else a - b

    def postfix(self, steps, pc):
        stack = []
        for kind, val in steps:
            if kind == 9:
                b, a = stack.pop(), stack.pop()
                stack.append({1: a + b, 2: a - b, 3: a * b,
                              4: a << b, 5: a >> b}[val])
            else:
                v = self.term(kind, val, pc)
                if v is None:
                    return None
                stack.append(v)
        return stack[-1] if stack else None

    def operand(self, rec, which, pc):
        """Value of operand `which` (1/2) expression, EXPR-aware."""
        for c in rec["conts"]:
            if c["kind"] == "EXPR" and c["which"] == which:
                return self.postfix(c["steps"], pc)
        return self.fields(rec["op1"] if which == 1 else rec["op2"], pc)

    def val0(self, rec, which, pc):
        """Operand value, unresolved -> 0 (for pass-1 sizing)."""
        v = self.operand(rec, which, pc)
        return 0 if v is None else v


# ---- effective-address encoding ----------------------------------------------

def s16_fits(v: int) -> bool:
    v &= 0xFFFFFFFF
    return v <= 0x7FFF or v >= 0xFFFF8000


class Ctx:
    """Per-record encoding context: phase, decisions, collected errors."""

    def __init__(self, ev: Eval, phase: int, errors: list):
        self.ev = ev
        self.phase = phase
        self.errors = errors
        self.dec: dict = {}     # frozen pass-1 decisions for this record
        self.pc = 0

    def addr_decision(self, rec, which):
        key = f"a{which}"
        if self.phase == 2:
            return self.dec.get(key, "L")
        if os.environ.get("PASS12_ADDR_POLICY") == "long":
            d = "L"
        else:
            v = self.ev.operand(rec, which, self.pc)
            d = "L" if v is None or not s16_fits(v) else "W"
        self.dec[key] = d
        return d

    def branch_decision(self, rec, target):
        if self.phase == 2:
            return self.dec.get("br", "W")
        if rec["suffix"] == 4:      # .S
            d = "S"
        elif rec["suffix"] == 2:    # .W
            d = "W"
        elif target is None:
            d = "W"
        else:
            disp = target - (self.pc + 2)
            d = "S" if -126 <= disp <= 129 and disp != 0 else "W"
        self.dec["br"] = d
        return d


def words_of(val: int, size: int):
    """Extension words for a long-sized value."""
    val &= 0xFFFFFFFF
    return [ (val >> 16) & 0xFFFF, val & 0xFFFF ]


def ea_encode(ctx: Ctx, rec, which: int, size: int):
    """-> (mode, reg, [ext words]). size: 0=B, 1=W, 2=L."""
    op = rec["op1"] if which == 1 else rec["op2"]
    t = op["type"]
    pc = ctx.pc
    if t == T_REGD:
        return 0, op["reg"], []
    if t == T_REGA:
        return 1, op["reg"], []
    if t == T_IND:
        return 2, op["reg"], []
    if t == T_POSTINC:
        return 3, op["reg"], []
    if t == T_PREDEC:
        return 4, op["reg"], []
    if t == T_DISP:
        d = ctx.ev.val0(rec, which, pc)
        return 5, op["reg"], [d & 0xFFFF]
    if t == T_INDEX:
        d = ctx.ev.val0(rec, which, pc)
        aux = rec["aux"] if which == 1 else (rec["flags"] & 0xF8)
        ext = ((0x8000 if aux & 0x80 else 0) | (((aux >> 4) & 7) << 12)
               | (0x0800 if aux & 0x08 else 0) | (d & 0xFF))
        return 6, op["reg"], [ext]
    if t == T_ADDR:
        v = ctx.ev.val0(rec, which, pc)
        if ctx.addr_decision(rec, which) == "W":
            return 7, 0, [v & 0xFFFF]
        return 7, 1, words_of(v, 2)
    if t == T_DISPPC:
        v = ctx.ev.val0(rec, which, pc)
        return 7, 2, [(v - (pc + 2)) & 0xFFFF]
    if t == T_INDEXPC:
        v = ctx.ev.val0(rec, which, pc)
        aux = rec["aux"] if which == 1 else (rec["flags"] & 0xF8)
        ext = ((0x8000 if aux & 0x80 else 0) | (((aux >> 4) & 7) << 12)
               | (0x0800 if aux & 0x08 else 0) | ((v - (pc + 2)) & 0xFF))
        return 7, 3, [ext]
    if t == T_IMM:
        v = ctx.ev.val0(rec, which, pc)
        if size == 0:
            return 7, 4, [v & 0xFF]
        if size == 1:
            return 7, 4, [v & 0xFFFF]
        return 7, 4, words_of(v, 2)
    raise ValueError(f"EA type {t} not encodable (line {rec['line']})")


def ea_bits(ctx, rec, which, size):
    mode, reg, ext = ea_encode(ctx, rec, which, size)
    return (mode << 3) | reg, ext


def imm_words(ctx, rec, which, size):
    v = ctx.ev.val0(rec, which, ctx.pc)
    if size == 0:
        return [v & 0xFF]
    if size == 1:
        return [v & 0xFFFF]
    return words_of(v, 2)


# ---- instruction encoding ------------------------------------------------------

COND = {"BRA": 0, "BSR": 1, "BHI": 2, "BLS": 3, "BCC": 4, "BCS": 5,
        "BNE": 6, "BEQ": 7, "BVC": 8, "BVS": 9, "BPL": 10, "BMI": 11,
        "BGE": 12, "BLT": 13, "BGT": 14, "BLE": 15}
DBCOND = {"DBF": 1, "DBNE": 6, "DBMI": 11}
IMM_GROUP = {"ORI": 0x0000, "ANDI": 0x0200, "SUBI": 0x0400,
             "ADDI": 0x0600, "EORI": 0x0A00, "CMPI": 0x0C00}
BIT_GROUP = {"BTST": (0x0800, 0x0100), "BCLR": (0x0880, 0x0180),
             "BSET": (0x08C0, 0x01C0)}
BIN_BASE = {"OR": 0x8000, "SUB": 0x9000, "AND": 0xC000, "ADD": 0xD000}
SHIFT_TYPE = {"ASR": 0, "ASL": 0, "LSR": 1, "LSL": 1,
              "ROXR": 2, "ROXL": 2, "ROR": 3, "ROL": 3}


def pack(words):
    return b"".join(struct.pack(">H", w & 0xFFFF) for w in words)


def rev16(m: int) -> int:
    return int(f"{m & 0xFFFF:016b}"[::-1], 2)


def encode_insn(ctx: Ctx, rec) -> bytes:
    name = rec["opcode"]
    size = {1: 0, 2: 1, 3: 2}.get(rec["suffix"], 1)   # B/W/L -> 0/1/2
    op1, op2 = rec["op1"], rec["op2"]
    pc = ctx.pc

    if name in ("MOVE", "MOVEA"):
        if op1["type"] == T_SPECIAL or op2["type"] == T_SPECIAL:
            if op1["type"] == T_SPECIAL:
                if op1["reg"] == 2:      # from USP
                    return pack([0x4E68 | op2["reg"]])
                base = 0x40C0 if op1["reg"] == 0 else 0x42C0   # SR / CCR
                bits, ext = ea_bits(ctx, rec, 2, 1)
                return pack([base | bits] + ext)
            if op2["reg"] == 2:          # to USP
                return pack([0x4E60 | op1["reg"]])
            base = 0x46C0 if op2["reg"] == 0 else 0x44C0
            bits, ext = ea_bits(ctx, rec, 1, 1)
            return pack([base | bits] + ext)
        sizebits = {0: 1, 1: 3, 2: 2}[size]
        sm, sr, sext = ea_encode(ctx, rec, 1, size)
        dm, dr, dext = ea_encode(ctx, rec, 2, size)
        return pack([(sizebits << 12) | (dr << 9) | (dm << 6) | (sm << 3) | sr]
                    + sext + dext)

    if name == "MOVEQ":
        imm = ctx.ev.val0(rec, 1, pc)
        return pack([0x7000 | (op2["reg"] << 9) | (imm & 0xFF)])

    if name in COND:
        target = ctx.ev.operand(rec, 1, pc)
        d = ctx.branch_decision(rec, target)
        t0 = target if target is not None else pc + 2
        disp = t0 - (pc + 2)
        if d == "S":
            return pack([0x6000 | (COND[name] << 8) | (disp & 0xFF)])
        return pack([0x6000 | (COND[name] << 8), disp & 0xFFFF])

    if name in DBCOND:
        target = ctx.ev.operand(rec, 2, pc)
        t0 = target if target is not None else pc + 2
        return pack([0x50C8 | (DBCOND[name] << 8) | op1["reg"],
                     (t0 - (pc + 2)) & 0xFFFF])

    if name in ("ADDQ", "SUBQ"):
        data = ctx.ev.val0(rec, 1, pc)
        bits, ext = ea_bits(ctx, rec, 2, size)
        return pack([(0x5000 if name == "ADDQ" else 0x5100)
                     | ((data & 7) << 9) | (size << 6) | bits] + ext)

    if name in IMM_GROUP:
        if op2["type"] == T_SPECIAL:
            table = {0: {"ORI": 0x007C, "ANDI": 0x027C, "EORI": 0x0A7C},
                     1: {"ORI": 0x003C, "ANDI": 0x023C, "EORI": 0x0A3C}}
            return pack([table[op2["reg"]][name],
                         ctx.ev.val0(rec, 1, pc) & 0xFFFF])
        bits, ext = ea_bits(ctx, rec, 2, size)
        return pack([IMM_GROUP[name] | (size << 6) | bits]
                    + imm_words(ctx, rec, 1, size) + ext)

    if name in BIT_GROUP:
        static, dyn = BIT_GROUP[name]
        if op1["type"] == T_IMM:
            bits, ext = ea_bits(ctx, rec, 2, 0)
            return pack([static | bits, ctx.ev.val0(rec, 1, pc) & 0xFFFF] + ext)
        bits, ext = ea_bits(ctx, rec, 2, 0)
        return pack([dyn | (op1["reg"] << 9) | bits] + ext)

    if name in ("ADD", "SUB", "AND", "OR", "CMP", "EOR"):
        if op1["type"] == T_IMM:
            # Macro Assembler quirk (measured against the golden dump):
            # an ALU mnemonic with an immediate source and a Dn
            # destination is encoded as the plain opcode with the
            # immediate in the EA field (AND.B #2,D0 -> $C03C $0002);
            # any other destination uses the immediate-group opcode.
            if op2["type"] == T_REGD and name in BIN_BASE or \
               op2["type"] == T_REGD and name == "CMP":
                base = BIN_BASE.get(name, 0xB000)
                return pack([base | (op2["reg"] << 9) | (size << 6) | 0x3C]
                            + imm_words(ctx, rec, 1, size))
            grp = {"OR": 0x0000, "AND": 0x0200, "SUB": 0x0400,
                   "ADD": 0x0600, "EOR": 0x0A00, "CMP": 0x0C00}[name]
            bits, ext = ea_bits(ctx, rec, 2, size)
            return pack([grp | (size << 6) | bits]
                        + imm_words(ctx, rec, 1, size) + ext)
        if op2["type"] == T_REGA:
            base = {"ADD": 0xD0C0, "SUB": 0x90C0, "CMP": 0xB0C0}[name]
            bits, ext = ea_bits(ctx, rec, 1, size)
            return pack([base | (op2["reg"] << 9)
                         | ((1 if size == 2 else 0) << 8) | bits] + ext)
        if name == "EOR":
            bits, ext = ea_bits(ctx, rec, 2, size)
            return pack([0xB100 | (op1["reg"] << 9) | (size << 6) | bits] + ext)
        if name == "CMP":
            bits, ext = ea_bits(ctx, rec, 1, size)
            return pack([0xB000 | (op2["reg"] << 9) | (size << 6) | bits] + ext)
        if op2["type"] == T_REGD:
            bits, ext = ea_bits(ctx, rec, 1, size)
            return pack([BIN_BASE[name] | (op2["reg"] << 9) | (size << 6) | bits] + ext)
        if op1["type"] == T_REGD:
            bits, ext = ea_bits(ctx, rec, 2, size)
            return pack([BIN_BASE[name] | (op1["reg"] << 9) | ((4 + size) << 6) | bits] + ext)
        raise ValueError(f"{name}: no Dn operand (line {rec['line']})")

    if name in ("ADDA", "SUBA", "CMPA"):
        base = {"ADDA": 0xD0C0, "SUBA": 0x90C0, "CMPA": 0xB0C0}[name]
        bits, ext = ea_bits(ctx, rec, 1, size)
        return pack([base | (op2["reg"] << 9) | ((1 if size == 2 else 0) << 8) | bits] + ext)

    if name in ("ADDX", "SUBX", "ABCD"):
        base = {"ADDX": 0xD100, "SUBX": 0x9100, "ABCD": 0xC100}[name]
        if name == "ABCD":
            size = 0        # ABCD is byte-only; the source omits the suffix
        rm = 1 if op1["type"] == T_PREDEC else 0
        return pack([base | (op2["reg"] << 9) | (size << 6) | (rm << 3) | op1["reg"]])

    if name == "CMPM":
        return pack([0xB100 | (op2["reg"] << 9) | (size << 6) | 0x08 | op1["reg"]])

    if name in SHIFT_TYPE:
        typ = SHIFT_TYPE[name]
        dirc = 1 if name.endswith("L") else 0
        if op2["type"] == T_NONE:            # memory form, single operand
            bits, ext = ea_bits(ctx, rec, 1, 1)
            return pack([0xE0C0 | (typ << 9) | (dirc << 8) | bits] + ext)
        if op1["type"] == T_IMM:
            cfield, ir = ctx.ev.val0(rec, 1, pc) & 7, 0
        elif op1["type"] == T_REGD:
            cfield, ir = op1["reg"], 1
        else:
            raise ValueError(f"{name}: bad count operand (line {rec['line']})")
        return pack([0xE000 | (cfield << 9) | (dirc << 8) | (size << 6)
                     | (ir << 5) | (typ << 3) | op2["reg"]])

    if name in ("CLR", "NEG", "NOT", "TST"):
        base = {"CLR": 0x4200, "NEG": 0x4400, "NOT": 0x4600, "TST": 0x4A00}[name]
        bits, ext = ea_bits(ctx, rec, 1, size)
        return pack([base | (size << 6) | bits] + ext)

    if name == "SWAP":
        return pack([0x4840 | op1["reg"]])
    if name == "EXT":
        return pack([(0x4880 if size == 1 else 0x48C0) | op1["reg"]])
    if name == "PEA":
        bits, ext = ea_bits(ctx, rec, 1, 1)
        return pack([0x4840 | bits] + ext)
    if name == "LEA":
        bits, ext = ea_bits(ctx, rec, 1, 1)
        return pack([0x41C0 | (op2["reg"] << 9) | bits] + ext)
    if name == "JMP":
        bits, ext = ea_bits(ctx, rec, 1, 1)
        return pack([0x4EC0 | bits] + ext)
    if name == "JSR":
        bits, ext = ea_bits(ctx, rec, 1, 1)
        return pack([0x4E80 | bits] + ext)
    if name == "RTS":
        return pack([0x4E75])

    if name == "MOVEM":
        sizebit = 1 if size == 2 else 0
        if op1["type"] == T_REGLIST:
            mask = op1["v1"]
            bits, ext = ea_bits(ctx, rec, 2, size)
            if op2["type"] == T_PREDEC:
                mask = rev16(mask)
            return pack([0x4880 | (sizebit << 6) | bits, mask] + ext)
        mask = op2["v1"]
        bits, ext = ea_bits(ctx, rec, 1, size)
        return pack([0x4C80 | (sizebit << 6) | bits, mask] + ext)

    if name in ("MULU", "MULS", "DIVU", "DIVS"):
        base = {"MULU": 0xC0C0, "MULS": 0xC1C0, "DIVU": 0x80C0, "DIVS": 0x81C0}[name]
        bits, ext = ea_bits(ctx, rec, 1, 1)
        return pack([base | (op2["reg"] << 9) | bits] + ext)

    if name == "EXG":
        t1, t2 = op1["type"], op2["type"]
        if t1 == T_REGD and t2 == T_REGD:
            mm, rx, ry = 0x08, op1["reg"], op2["reg"]
        elif t1 == T_REGA and t2 == T_REGA:
            mm, rx, ry = 0x09, op1["reg"], op2["reg"]
        elif t1 == T_REGD and t2 == T_REGA:
            mm, rx, ry = 0x11, op1["reg"], op2["reg"]
        elif t1 == T_REGA and t2 == T_REGD:
            mm, rx, ry = 0x11, op2["reg"], op1["reg"]
        else:
            raise ValueError(f"EXG: bad operands (line {rec['line']})")
        return pack([0xC100 | (rx << 9) | (mm << 3) | ry])

    raise ValueError(f"unhandled opcode {name} (line {rec['line']})")


# ---- DC / DS -------------------------------------------------------------------

def suffix_bytes(suffix: int) -> int:
    return {1: 1, 2: 2, 3: 4}.get(suffix, 2)


def dc_bytes(ctx: Ctx, rec) -> bytes:
    szb = suffix_bytes(rec["suffix"])
    mask = (1 << (8 * szb)) - 1
    out = bytearray()

    def emit_item(op):
        v = ctx.ev.fields(op, ctx.pc)
        out.extend(((0 if v is None else v) & mask).to_bytes(szb, "big"))

    if rec["op1"]["type"] == T_ITEM:
        v = ctx.ev.operand(rec, 1, ctx.pc)
        out.extend(((0 if v is None else v) & mask).to_bytes(szb, "big"))
    for c in rec["conts"]:
        if c["kind"] == "ITEM":
            emit_item(c["op"])
        elif c["kind"] == "STR":
            out.extend(c["data"])
    return bytes(out)


# ---- sweeps ----------------------------------------------------------------------

def fixpoint_equ(equ_recs, ev: Eval, values):
    pending = [r for r in equ_recs if values[r["label"]] is None]
    progress = True
    while pending and progress:
        progress = False
        rest = []
        for r in pending:
            v = ev.operand(r, 1, 0)
            if v is None:
                rest.append(r)
            else:
                values[r["label"]] = v & 0xFFFFFFFF
                progress = True
        pending = rest
    return pending


def pass1(mains, ev, values, decisions, errors):
    equ_recs = [r for r in mains if r["opcode"] == "EQU"]
    fixpoint_equ(equ_recs, ev, values)
    rec_addr = {}
    LC = 0
    for rec in mains:
        name = rec["opcode"]
        rec_addr[rec["idx"]] = LC
        if rec["label"] and name != "EQU":
            values[rec["label"]] = LC
        if name is None or name in ("EQU", "CPU", "SUPMODE"):
            continue
        if name == "END":
            break
        if name == "ORG":
            v = ev.operand(rec, 1, LC)
            if v is not None:
                LC = v
                rec_addr[rec["idx"]] = LC
            continue
        if name == "ALIGN":
            b = ev.operand(rec, 1, LC) or 2
            LC += (-LC) % b
            continue
        if name == "DS":
            cnt = ev.operand(rec, 1, LC)
            if cnt is None:
                errors.append(f"DS count unresolved (line {rec['line']})")
                cnt = 0
            LC += cnt * suffix_bytes(rec["suffix"])
            continue
        ctx = Ctx(ev, 1, errors)
        ctx.pc = LC
        ctx.dec = decisions[rec["idx"]]
        try:
            if name == "DC":
                LC += len(dc_bytes(ctx, rec))
            else:
                LC += len(encode_insn(ctx, rec))
        except ValueError as e:
            errors.append(str(e))
            LC += 2
    fixpoint_equ(equ_recs, ev, values)
    return rec_addr


def pass2(mains, ev, values, decisions, errors):
    image = {}
    LC = 0
    for rec in mains:
        name = rec["opcode"]
        if name is None or name in ("EQU", "CPU", "SUPMODE"):
            continue
        if name == "END":
            break
        if name == "ORG":
            v = ev.operand(rec, 1, LC)
            if v is not None:
                LC = v
            continue
        if name == "ALIGN":
            b = ev.operand(rec, 1, LC) or 2
            while LC % b:
                image[LC] = 0x00
                LC += 1
            continue
        if name == "DS":
            cnt = ev.operand(rec, 1, LC)
            LC += (cnt or 0) * suffix_bytes(rec["suffix"])
            continue
        ctx = Ctx(ev, 2, errors)
        ctx.pc = LC
        ctx.dec = decisions[rec["idx"]]
        try:
            data = dc_bytes(ctx, rec) if name == "DC" else encode_insn(ctx, rec)
        except ValueError as e:
            errors.append("pass2: " + str(e))
            data = b"\x00\x00"
        for i, byte in enumerate(data):
            image[LC + i] = byte
        LC += len(data)
    return image, (max(image) + 1 if image else 0)


# ---- golden reference --------------------------------------------------------------

def load_golden() -> bytes:
    path = os.path.join(HERE, "..", "research", "EhBASIC68k-EMU", "EhBASICv354.txt")
    data = []
    with open(path) as f:
        for ln in f:
            if ln.strip().startswith("//"):
                continue
            data += [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", ln)]
    return bytes(data)


def main() -> None:
    symbols, kinds, opcodes = load_tables()
    with open(os.path.join(OUT, "intermediate.bin"), "rb") as f:
        mains = parse_records(f.read(), opcodes)
    with open(os.path.join(OUT, "formatted.txt")) as f:
        fmt_lines = f.read().splitlines()
    values = [None] * (len(symbols) + 1)
    ev = Eval(values)
    # Pass 1 runs to a fixpoint: forward branch sizes and address
    # widths are decided with the previous iteration's symbol values,
    # like a multi-pass assembler, until nothing moves.
    decisions, rec_addr, errors = {}, {}, []
    prev_snapshot = None
    iterations = 0
    for iterations in range(1, 13):
        decisions = {r["idx"]: {} for r in mains}
        errors = []
        rec_addr = pass1(mains, ev, values, decisions, errors)
        snapshot = (tuple(values),
                    tuple(sorted((k, tuple(sorted(v.items())))
                                 for k, v in decisions.items())))
        if snapshot == prev_snapshot:
            break
        prev_snapshot = snapshot
    image, end = pass2(mains, ev, values, decisions, errors)
    with open(os.path.join(OUT, "image.bin"), "wb") as f:
        f.write(bytes(image.get(i, 0xFF) for i in range(end)))
    with open(os.path.join(OUT, "symbols_values.txt"), "w") as f:
        for i, name in enumerate(symbols, 1):
            v = values[i]
            f.write(f"{i} {name} {'$%08X' % v if v is not None else 'UNRESOLVED'}\n")

    golden = load_golden()
    ours = bytes(image.get(i, 0xFF if i < end else 0x00) for i in range(len(golden)))
    mism = [i for i in range(len(golden)) if ours[i] != golden[i]]
    runs = []
    for a in mism:
        if runs and a == runs[-1][1] + 1:
            runs[-1][1] = a
        else:
            runs.append([a, a])
    addr_of_label = {}
    for i, name in enumerate(symbols, 1):
        if values[i] is not None:
            addr_of_label.setdefault(values[i], []).append(name)
    code_recs = sorted((a, idx) for idx, a in rec_addr.items() if a >= 0xC0)

    def context(addr):
        import bisect
        pos = bisect.bisect_right(code_recs, (addr, 1 << 30)) - 1
        if pos < 0:
            return ""
        _a, idx = code_recs[pos]
        line = mains[idx]["line"]
        lab = addr_of_label.get(addr)
        s = f"line {line}: {fmt_lines[line - 1].strip()}"
        if lab:
            s += f"   [{'/'.join(lab)} @ ${addr:04X}]"
        return s

    unresolved = [symbols[i - 1] for i in range(1, len(symbols) + 1)
                  if values[i] is None]
    n_valued = sum(1 for v in values[1:] if v is not None)
    report = []
    report.append("# Pass 1+2 prototype -- measured results\n")
    report.append("## Assembly\n")
    report.append(f"- symbols valued: {n_valued} / {len(symbols)}; unresolved: {unresolved}")
    report.append(f"- pass 1 iterations to fixpoint: {iterations}")
    report.append(f"- image end: ${end:04X} (golden content ends $35DB, image $4000)")
    report.append(f"- errors: {len(errors)}")
    for e in errors[:20]:
        report.append(f"  - {e}")
    report.append("\n## Diff vs golden EhBASICv354\n")
    report.append(f"- mismatched bytes: {len(mism)} / {len(golden)} "
                  f"({100.0 * (len(golden) - len(mism)) / len(golden):.2f}% match)")
    report.append(f"- mismatch runs: {len(runs)}")
    for a, b in runs[:40]:
        report.append(f"  - ${a:04X}-${b:04X} ({b - a + 1} bytes)  {context(a)}")
    report.append("\n## First mismatches\n")
    for a in mism[:25]:
        report.append(f"- ${a:04X}: ours ${ours[a]:02X} vs golden ${golden[a]:02X}  {context(a)}")
    text = "\n".join(report) + "\n"
    with open(os.path.join(OUT, "report12.md"), "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
