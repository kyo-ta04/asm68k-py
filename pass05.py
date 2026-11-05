"""P2: Pass 0.5 prototype -- formatted tape -> fixed-length records.

Implements proto/record-layout.md v1:
  - 32-byte fixed records on the intermediate tape (slot 3)
  - two sweeps over the formatted tape (slot 2): sweep 1 collects
    symbol definitions and the opcode table, sweep 2 emits records
  - expressions are (term)+-(term); symbol-free complex expressions
    are constant-folded; the few symbol-bearing complex expressions
    (all in immediate operands) become postfix EXPR continuations
  - DC items beyond the first become ITEM/STR continuations

Operand shapes come from recon05.classify, the same classifier the
census used, so the encoder and the census cannot drift apart.
"""

from __future__ import annotations

import ast
import os
import re
import struct
from collections import Counter

import tapedeck as td
from pass0 import TapeLineReader
from recon05 import split_operands, classify

SLOT_FORMATTED = 2
SLOT_INTERMEDIATE = 3
REC = 32
CONT = 0xFF

# op_type codes (record-layout.md)
T_NONE, T_REGD, T_REGA, T_IND, T_POSTINC, T_PREDEC, T_DISP, T_INDEX, \
    T_DISPPC, T_INDEXPC, T_ADDR, T_IMM, T_REGLIST, T_SPECIAL, T_STR, \
    T_ITEM = range(16)

K_NONE, K_CONST, K_SYM, K_CURPC = 0, 1, 2, 3
SUFFIX = {"": 0, "B": 1, "W": 2, "L": 3, "S": 4}
TOKEN_RE = re.compile(r"\S+")
TERM_RE = r"(?:\$[0-9A-F]+|[0-9]+|'[^']*'|[A-Z_][A-Z0-9_.$]*|\*)"
SIMPLE_EXPR = re.compile(rf"^([+-]?)({TERM_RE})(?:([+-])({TERM_RE}))?$")
REGTOK = r"(?:[DA][0-7]|SP)"


def reg_num(tok: str) -> int:
    return 7 if tok == "SP" else int(tok[1])


def parse_formatted(line: str):
    has_label = line[0] != " "
    body = line if has_label else line[1:]
    spans = list(TOKEN_RE.finditer(body))
    tokens = [m.group(0) for m in spans]
    label = mnemonic = None
    operand = ""
    if has_label:
        label = tokens[0]
        if len(tokens) > 1:
            mnemonic = tokens[1]
            operand = body[spans[1].end():].strip()
    else:
        mnemonic = tokens[0]
        operand = body[spans[0].end():].strip()
    base = suffix = None
    if mnemonic:
        base, _, suffix = mnemonic.partition(".")
    return label, base, suffix, operand


class SymbolTable:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.id_of: dict[str, int] = {}
        self.kind: dict[int, str] = {}   # 'L' label, 'E' equ, 'U' undefined-ref
        self.duplicates: list[str] = []

    def _assign(self, name: str, kind: str) -> int:
        sid = len(self.names) + 1
        self.names.append(name)
        self.id_of[name] = sid
        self.kind[sid] = kind
        return sid

    def define(self, name: str, kind: str) -> int:
        if name in self.id_of:
            sid = self.id_of[name]
            if self.kind[sid] == "U":
                self.kind[sid] = kind
            else:
                self.duplicates.append(name)
            return sid
        return self._assign(name, kind)

    def ref(self, name: str) -> int:
        if name in self.id_of:
            return self.id_of[name]
        return self._assign(name, "U")

    def name_of(self, sid: int) -> str:
        return self.names[sid - 1] if 1 <= sid <= len(self.names) else f"?{sid}"


class ExprEncoder:
    """Encodes expression strings into term fields / continuations."""

    def __init__(self, symbols: SymbolTable) -> None:
        self.symbols = symbols
        self.folded = 0
        self.complex: list[tuple[str, str]] = []   # (expr, where)
        self.failed: list[tuple[str, str]] = []

    def term(self, tok: str):
        """-> (kind, value); symbol values are IDs (resolved now)."""
        if tok.startswith("$"):
            return K_CONST, int(tok[1:], 16)
        if tok[0].isdigit():
            return K_CONST, int(tok, 10)
        if tok.startswith("'"):
            chars = tok[1:-1]
            v = 0
            for ch in chars:
                v = (v << 8) | ord(ch)
            return K_CONST, v
        if tok == "*":
            return K_CURPC, 0
        return K_SYM, self.symbols.ref(tok)

    def try_simple(self, expr: str):
        """-> (k1, v1, op, k2, v2) or None. op: 0 none, 1 +, 2 -."""
        m = SIMPLE_EXPR.fullmatch(expr)
        if not m:
            return None
        sign, t1, op, t2 = m.groups()
        k1, v1 = self.term(t1)
        if sign == "-":
            if k1 == K_CONST:
                v1 = (-v1) & 0xFFFFFFFF
            elif t2 is None:
                # -(symbol) == 0 - symbol
                return (K_CONST, 0, 2, k1, v1)
            else:
                return None  # -(sym) +- term: beyond the simple form
        if t2 is None:
            return (k1, v1, 0, K_NONE, 0)
        k2, v2 = self.term(t2)
        return (k1, v1, 1 if op == "+" else 2, k2, v2)

    def fold_const(self, expr: str):
        """Evaluate a symbol-free expression; -> int or None."""
        probe = re.sub(r"\$[0-9A-F]+", "", expr)
        probe = re.sub(r"'[^']*'", "", probe)
        if re.search(r"[A-Za-z_]", probe):
            return None
        py = re.sub(r"\$([0-9A-F]+)", r"0x\1", expr)
        py = re.sub(r"'([^']*)'", lambda m: str(self.term(m.group(0))[1]), py)
        try:
            tree = ast.parse(py, mode="eval")
        except SyntaxError:
            return None

        def ev(node):
            if isinstance(node, ast.Expression):
                return ev(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, int):
                return node.value
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
                v = ev(node.operand)
                return -v if isinstance(node.op, ast.USub) else v
            if isinstance(node, ast.BinOp):
                a, b = ev(node.left), ev(node.right)
                if isinstance(node.op, ast.Add):
                    return a + b
                if isinstance(node.op, ast.Sub):
                    return a - b
                if isinstance(node.op, ast.Mult):
                    return a * b
                if isinstance(node.op, ast.LShift):
                    return a << b
                if isinstance(node.op, ast.RShift):
                    return a >> b
            raise ValueError("not constant")

        try:
            return ev(tree)
        except (ValueError, TypeError):
            return None

    def to_postfix(self, expr: str):
        """-> list of (kind, value) steps, kind 9 = operator."""
        toks = re.findall(r"\$[0-9A-F]+|[0-9]+|'[^']*'|[A-Z_][A-Z0-9_.$]*|\*|<<|>>|[-+()]", expr)
        prec = {"<<": 1, ">>": 1, "+": 2, "-": 2, "*": 3}
        op_code = {"+": 1, "-": 2, "*": 3, "<<": 4, ">>": 5}
        out, stack = [], []
        prev = None  # None | 'term' | 'op' | '('
        for tok in toks:
            if tok in prec:
                if prev in (None, "op", "(") and tok in "+-":
                    if tok == "-":
                        out.append((K_CONST, 0))
                        stack.append("-")
                        prev = "op"
                    continue
                while stack and stack[-1] in prec and prec[stack[-1]] >= prec[tok]:
                    out.append((9, op_code[stack.pop()]))
                stack.append(tok)
                prev = "op"
            elif tok == "(":
                stack.append(tok)
                prev = "("
            elif tok == ")":
                while stack and stack[-1] != "(":
                    out.append((9, op_code[stack.pop()]))
                if not stack:
                    return None
                stack.pop()
                prev = "term"
            else:
                out.append(self.term(tok))
                prev = "term"
        while stack:
            if stack[-1] == "(":
                return None
            out.append((9, op_code[stack.pop()]))
        return out


def put_op(rec: bytearray, base: int, otype: int, reg: int, fields, aux_holder: list):
    rec[base] = otype
    rec[base + 1] = reg
    if fields is not None:
        k1, v1, op, k2, v2 = fields
        rec[base + 2] = k1
        struct.pack_into("<I", rec, base + 3, v1 & 0xFFFFFFFF)
        rec[base + 7] = op
        rec[base + 8] = k2
        struct.pack_into("<I", rec, base + 9, v2 & 0xFFFFFFFF)


class Pass05:
    def __init__(self, deck: td.TapeDeck) -> None:
        self.deck = deck
        self.symbols = SymbolTable()
        self.opcodes: list[str] = []
        self.op_id: dict[str, int] = {}
        self.expr = ExprEncoder(self.symbols)
        self.records: list[bytearray] = []
        self.line_record: dict[int, int] = {}
        self.stats = Counter()
        self.complex_exprs: list[tuple[int, str, str]] = []
        self.unencodable: list[tuple[int, str]] = []

    # ---- tape line access ------------------------------------------------
    def read_lines(self):
        self.deck.select(SLOT_FORMATTED)
        self.deck.rewind()
        reader = TapeLineReader(self.deck, SLOT_FORMATTED)
        lines = []
        while True:
            line = reader.read_line()
            if line is None:
                break
            lines.append(line)
        return lines

    # ---- sweeps ----------------------------------------------------------
    def sweep1(self, lines) -> None:
        for line in lines:
            label, base, _suffix, _operand = parse_formatted(line)
            if base and base not in self.op_id:
                self.op_id[base] = len(self.opcodes) + 1
                self.opcodes.append(base)
            if label:
                self.symbols.define(label, "E" if base == "EQU" else "L")

    def encode_expr_fields(self, expr: str, where: str):
        """-> (fields, is_complex). fields None when complex."""
        simple = self.expr.try_simple(expr)
        if simple is not None:
            return simple, False
        folded = self.expr.fold_const(expr)
        if folded is not None:
            self.expr.folded += 1
            self.stats["expr_folded"] += 1
            return (K_CONST, folded & 0xFFFFFFFF, 0, K_NONE, 0), False
        self.complex_exprs.append((where, expr))
        return None, True

    def expr_cont_record(self, which: int, expr: str) -> bytearray:
        rec = bytearray(REC)
        rec[2] = CONT
        rec[3] = 3
        rec[5] = which
        steps = self.expr.to_postfix(expr)
        if steps is None or len(steps) > 5:
            self.unencodable.append((expr, "postfix"))
            steps = steps or []
        rec[6] = len(steps)
        for i, (kind, val) in enumerate(steps[:5]):
            rec[7 + i * 5] = kind
            struct.pack_into("<I", rec, 8 + i * 5, val & 0xFFFFFFFF)
        return rec

    def item_cont_record(self, expr: str, where: str) -> bytearray:
        rec = bytearray(REC)
        rec[2] = CONT
        rec[3] = 1
        fields, complex = self.encode_expr_fields(expr, where)
        if complex:
            # DC items with symbol-bearing complex exprs do not occur in
            # this corpus; if one appears we want to know loudly.
            self.unencodable.append((where, "dc-item-complex:" + expr))
            fields = (K_NONE, 0, 0, K_NONE, 0)
        rec[5] = T_ITEM
        put_op(rec, 5, T_ITEM, 0, fields, [])
        return rec

    def str_cont_records(self, data: bytes) -> list[bytearray]:
        recs = []
        for off in range(0, len(data), 24):
            chunk = data[off : off + 24]
            rec = bytearray(REC)
            rec[2] = CONT
            rec[3] = 2
            rec[7 : 7 + len(chunk)] = chunk
            rec[31] = len(chunk)
            recs.append(rec)
        return recs

    def encode_operand(self, op_str: str, rec: bytearray, which: int, where: str):
        """Fill op fields of rec; return EXPR continuation or None."""
        base = 5 if which == 1 else 18
        shape, _detail = classify(op_str)
        cont = None

        def expr_fields(expr: str):
            nonlocal cont
            fields, complex = self.encode_expr_fields(expr, where)
            if complex:
                cont = self.expr_cont_record(which, expr)
                return None
            return fields

        if shape == "REG":
            put_op(rec, base, T_REGD if op_str[0] == "D" else T_REGA,
                   reg_num(op_str) if op_str != "SP" else 7, None, [])
            if op_str == "SP":
                rec[base] = T_REGA
                rec[base + 1] = 7
        elif shape == "SPECIAL":
            put_op(rec, base, T_SPECIAL, {"SR": 0, "CCR": 1, "USP": 2}[op_str], None, [])
        elif shape == "REGLIST":
            mask = 0
            for part in op_str.split("/"):
                if "-" in part:
                    a, b = part.split("-")
                    lo, hi = reg_num(a), reg_num(b)
                    bank = 0 if a[0] == "D" else 8
                    for r in range(lo, hi + 1):
                        mask |= 1 << (bank + r)
                else:
                    bank = 0 if part[0] == "D" else 8
                    mask |= 1 << (bank + reg_num(part))
            rec[base] = T_REGLIST
            struct.pack_into("<I", rec, base + 3, mask)
        elif shape == "IMM":
            put_op(rec, base, T_IMM, 0, expr_fields(op_str[1:]), [])
        elif shape in ("IND", "POSTINC", "PREDEC"):
            tok = re.search(rf"\(({REGTOK})\)", op_str).group(1)
            put_op(rec, base, {"IND": T_IND, "POSTINC": T_POSTINC, "PREDEC": T_PREDEC}[shape],
                   reg_num(tok), None, [])
        elif shape == "DISP":
            m = re.fullmatch(rf"(.*)\(({REGTOK})\)", op_str)
            put_op(rec, base, T_DISP, reg_num(m.group(2)), expr_fields(m.group(1)), [])
        elif shape == "DISPPC":
            m = re.fullmatch(r"(.*)\(PC\)", op_str)
            put_op(rec, base, T_DISPPC, 0, expr_fields(m.group(1)), [])
        elif shape in ("INDEX", "INDEXPC"):
            if shape == "INDEX":
                m = re.fullmatch(rf"(.*)\(({REGTOK}),([DA][0-7])(?:\.([WL]))?\)", op_str)
                basetok, idxtok, idxsize = m.group(2), m.group(3), m.group(4)
                reg = reg_num(basetok)
                otype = T_INDEX
            else:
                m = re.fullmatch(r"(.*)\(PC,([DA][0-7])(?:\.([WL]))?\)", op_str)
                idxtok, idxsize = m.group(2), m.group(3)
                reg = 0
                otype = T_INDEXPC
            disp = m.group(1)
            fields = None
            if disp not in ("", "0"):
                fields = expr_fields(disp)
            elif disp == "0":
                fields = (K_CONST, 0, 0, K_NONE, 0)
            put_op(rec, base, otype, reg, fields, [])
            aux = (0x80 if idxtok[0] == "A" else 0) | (int(idxtok[1]) << 4) | (0x08 if idxsize == "L" else 0)
            if which == 1:
                rec[31] = aux
            else:
                # op2's index info lives in the flags byte's free bits
                # (record-layout v1.1: one aux byte was not enough --
                # MOVE.B (A1,D2.W),(A0,D4.W) needs both)
                rec[4] |= aux & 0xF8
        elif shape == "ADDR":
            put_op(rec, base, T_ADDR, 0, expr_fields(op_str), [])
        else:
            self.unencodable.append((where, op_str))
            rec[base] = T_NONE
        return cont

    def sweep2(self, lines) -> None:
        for lineno, line in enumerate(lines, 1):
            where = f"fmt:{lineno}"
            label, base, suffix, operand = parse_formatted(line)
            rec = bytearray(REC)
            self.line_record[lineno] = len(self.records)
            if label:
                rec[0:2] = struct.pack("<H", self.symbols.id_of[label])
            conts: list[bytearray] = []
            if base is None:
                rec[2] = 0  # label-only line
            else:
                rec[2] = self.op_id[base]
                rec[3] = SUFFIX.get(suffix or "", 0)
                if suffix and suffix not in SUFFIX:
                    self.stats["suffix_unknown"] += 1
                if base == "EQU":
                    fields, complex = self.encode_expr_fields(operand, where)
                    if complex:
                        self.unencodable.append((where, "equ-complex"))
                        fields = (K_NONE, 0, 0, K_NONE, 0)
                    put_op(rec, 5, T_ITEM, 0, fields, [])
                elif base == "ORG":
                    fields, complex = self.encode_expr_fields(operand, where)
                    if complex:
                        self.unencodable.append((where, "org-complex"))
                        fields = (K_NONE, 0, 0, K_NONE, 0)
                    put_op(rec, 5, T_ITEM, 0, fields, [])
                elif base in ("DC", "DS"):
                    items = split_operands(operand) if operand else []
                    dc_szb = {"B": 1, "W": 2, "L": 4}.get(suffix or "W", 2)
                    for i, item in enumerate(items):
                        quoted = len(item) >= 2 and item[0] == item[-1] \
                            and item[0] in "'\""
                        # A quoted item longer than the item size is a
                        # string (DC.B 'Array bounds'), not a packed
                        # character constant (DC.B 'E', DC.W 'AB').
                        if item.startswith('"') and item.endswith('"') or \
                           quoted and len(item[1:-1]) > dc_szb:
                            data = item[1:-1].encode("latin-1")
                            if i == 0:
                                rec[5] = T_STR
                            conts += self.str_cont_records(data)
                        elif i == 0:
                            fields, complex = self.encode_expr_fields(item, where)
                            if complex:
                                self.unencodable.append((where, "dc0-complex"))
                                fields = (K_NONE, 0, 0, K_NONE, 0)
                            put_op(rec, 5, T_ITEM, 0, fields, [])
                        else:
                            conts.append(self.item_cont_record(item, where))
                    self.stats["dc_items"] += len(items)
                elif base in ("CPU", "SUPMODE", "EVEN", "END"):
                    pass  # opcode only; CPU/SUPMODE operands are dropped by design
                else:
                    ops = split_operands(operand) if operand else []
                    expr_conts = []
                    for i, op_str in enumerate(ops[:2]):
                        c = self.encode_operand(op_str, rec, i + 1, where)
                        if c is not None:
                            expr_conts.append(c)
                    conts = expr_conts + conts
            chain = [rec] + conts
            for i, r in enumerate(chain):
                if i < len(chain) - 1:
                    r[4] |= 1
                self.records.append(r)
            self.stats["main_records"] += 1
            self.stats["cont_records"] += len(conts)

    def run(self, lines) -> None:
        self.sweep1(lines)
        self.sweep2(lines)
        self.deck.select(SLOT_INTERMEDIATE)
        self.deck.new_tape()
        for rec in self.records:
            for b in rec:
                self.deck.write_byte(b)


# ---- record decoding (for spot checks / listings) -------------------------

TYPE_NAMES = {
    T_NONE: "NONE", T_REGD: "REGD", T_REGA: "REGA", T_IND: "IND",
    T_POSTINC: "POSTINC", T_PREDEC: "PREDEC", T_DISP: "DISP",
    T_INDEX: "INDEX", T_DISPPC: "DISPPC", T_INDEXPC: "INDEXPC",
    T_ADDR: "ADDR", T_IMM: "IMM", T_REGLIST: "REGLIST",
    T_SPECIAL: "SPECIAL", T_STR: "STR", T_ITEM: "ITEM",
}
SUF_NAMES = {0: "", 1: ".B", 2: ".W", 3: ".L", 4: ".S"}


def fmt_term(kind: int, val: int, symbols: SymbolTable) -> str:
    if kind == K_NONE:
        return ""
    if kind == K_CONST:
        return str(val) if val < 10 else "$%X" % val
    if kind == K_SYM:
        return symbols.name_of(val)
    if kind == K_CURPC:
        return "*"
    return f"?k{kind}"


def fmt_expr(rec, base: int, symbols: SymbolTable) -> str:
    k1, v1 = rec[base + 2], struct.unpack_from("<I", rec, base + 3)[0]
    op = rec[base + 7]
    k2, v2 = rec[base + 8], struct.unpack_from("<I", rec, base + 9)[0]
    s = fmt_term(k1, v1, symbols)
    if op:
        s += ("+" if op == 1 else "-") + fmt_term(k2, v2, symbols)
    return s


def fmt_op(rec, base: int, symbols: SymbolTable) -> str:
    otype = rec[base]
    if otype == T_NONE:
        return "-"
    name = TYPE_NAMES.get(otype, f"?t{otype}")
    reg = rec[base + 1]
    if otype in (T_REGD, T_REGA):
        return f"{name} {'D' if otype == T_REGD else 'A'}{reg}"
    if otype in (T_IND, T_POSTINC, T_PREDEC, T_DISP, T_INDEX):
        s = f"{name} A{reg} expr[{fmt_expr(rec, base, symbols)}]"
        if otype == T_INDEX:
            aux = rec[31]
            s += f" idx[{'A' if aux & 0x80 else 'D'}{(aux >> 4) & 7}{'.L' if aux & 0x08 else '.W'}]"
        return s
    if otype == T_REGLIST:
        mask = struct.unpack_from("<I", rec, base + 3)[0]
        regs = [("D%d" % i if i < 8 else "A%d" % (i - 8)) for i in range(16) if mask & (1 << i)]
        return f"REGLIST {','.join(regs)}"
    if otype == T_SPECIAL:
        return f"SPECIAL {['SR', 'CCR', 'USP'][reg]}"
    return f"{name} expr[{fmt_expr(rec, base, symbols)}]"


def decode_record(rec, symbols: SymbolTable, opcodes: list[str]) -> str:
    label_id = struct.unpack_from("<H", rec, 0)[0]
    label = symbols.name_of(label_id) if label_id else "-"
    opcode = rec[2]
    if opcode == CONT:
        kind = rec[3]
        if kind == 1:
            return f"CONT-ITEM {fmt_expr(rec, 5, symbols)}"
        if kind == 2:
            n = rec[31]
            return f"CONT-STR {bytes(rec[7:7 + n])!r}"
        if kind == 3:
            steps = []
            for i in range(rec[6]):
                k = rec[7 + i * 5]
                v = struct.unpack_from("<I", rec, 8 + i * 5)[0]
                if k == 9:
                    steps.append({1: "+", 2: "-", 3: "*", 4: "<<", 5: ">>"}[v])
                else:
                    steps.append(fmt_term(k, v, symbols))
            return f"CONT-EXPR op{rec[5]} postfix[{' '.join(steps)}]"
        return f"CONT? kind={kind}"
    opname = opcodes[opcode - 1] if 1 <= opcode <= len(opcodes) else "(label-only)"
    return (f"{label} {opname}{SUF_NAMES.get(rec[3], '?')} "
            f"op1[{fmt_op(rec, 5, symbols)}] op2[{fmt_op(rec, 18, symbols)}]"
            f"{' EXT' if rec[4] & 1 else ''}")


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, "out")
    deck = td.TapeDeck()
    with open(os.path.join(outdir, "formatted.txt"), "rb") as f:
        deck.load_bytes(SLOT_FORMATTED, f.read())
    p = Pass05(deck)
    lines = p.read_lines()
    p.run(lines)
    deck.save_file(SLOT_INTERMEDIATE, os.path.join(outdir, "intermediate.bin"))
    with open(os.path.join(outdir, "symbols.txt"), "w") as f:
        for i, name in enumerate(p.symbols.names, 1):
            f.write(f"{i} {name} {p.symbols.kind[i]}\n")
    with open(os.path.join(outdir, "opcodes.txt"), "w") as f:
        for i, name in enumerate(p.opcodes, 1):
            f.write(f"{i} {name}\n")
    total_bytes = deck.length_of(SLOT_INTERMEDIATE)
    undefined = [n for i, n in enumerate(p.symbols.names, 1) if p.symbols.kind[i] == "U"]
    cont_kinds = Counter()
    for rec in p.records:
        if rec[2] == CONT:
            cont_kinds[rec[3]] += 1

    def find_line(pred, desc):
        for i, line in enumerate(lines, 1):
            if pred(line):
                return i
        return None

    spot_targets = [
        ("DS.W with label", lambda l: l.startswith("LAB_WARM DS.W")),
        ("quoted ';' immediate", lambda l: "CMP.B #';'" in l),
        ("MOVEM reglist", lambda l: " MOVEM" in l or l.startswith("MOVEM")),
        ("folded DC.W 255<<8", lambda l: "DC.W 255<<8" in l),
        ("complex immediate (EXPR cont)", lambda l: "#(TK_LT-TK_PLUS)*4" in l),
        ("DC.B two items", lambda l: " DC.B '" in l and "," in l),
        ("branch to label", lambda l: " BSR " in l or l.startswith(" BSR")),
    ]
    spot_lines = []
    for desc, pred in spot_targets:
        ln = find_line(pred, desc)
        if ln is None:
            spot_lines.append(f"- {desc}: NOT FOUND")
            continue
        idx = p.line_record[ln]
        decoded = []
        j = idx
        while True:
            decoded.append(decode_record(p.records[j], p.symbols, p.opcodes))
            if not (p.records[j][4] & 1):
                break
            j += 1
        spot_lines.append(f"- {desc} (formatted line {ln}: {lines[ln - 1].strip()})")
        for d in decoded:
            spot_lines.append(f"    -> {d}")

    report = []
    report.append("# Pass 0.5 prototype -- measured results\n")
    report.append("## Records\n")
    report.append(f"- main records: {p.stats['main_records']} (formatted lines: {len(lines)})")
    report.append(f"- continuations: ITEM {cont_kinds[1]}, STR {cont_kinds[2]}, EXPR {cont_kinds[3]}")
    report.append(f"- total records: {len(p.records)} x 32B = **{total_bytes} bytes** on slot 3")
    report.append(f"- intermediate BSD sum: {td.format_sum(deck.dump_bytes(SLOT_INTERMEDIATE))}")
    report.append("\n## Symbols and opcodes\n")
    kinds = Counter(p.symbols.kind.values())
    report.append(f"- symbols: {len(p.symbols.names)} total (labels {kinds['L']}, EQU {kinds['E']}, undefined {kinds['U']})")
    report.append(f"- undefined at end: {undefined}")
    report.append(f"- duplicate definitions: {p.symbols.duplicates}")
    report.append(f"- opcodes: {len(p.opcodes)} (table in opcodes.txt)")
    report.append("\n## Expressions\n")
    report.append(f"- constant-folded: {p.stats['expr_folded']}")
    report.append(f"- complex (EXPR continuations): {len(p.complex_exprs)}")
    for where, expr in p.complex_exprs:
        report.append(f"  - {expr}  ({where})")
    report.append(f"- unencodable: {p.unencodable}")
    report.append("\n## Spot checks (record decodes)\n")
    report.extend(spot_lines)
    text = "\n".join(report) + "\n"
    with open(os.path.join(outdir, "report05.md"), "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
