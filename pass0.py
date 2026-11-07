"""P1: Pass 0 prototype -- formatting + INCLUDE expansion + EQU collection.

Reads the main source tape, writes the formatted tape, following the
pass design in ../asm-pass-design.md and the constraints the BASIC
version will live under:
  - byte-stream input only (line = bytes up to LF; CR tolerated,
    ^Z acts as end of text -- the insurance decided 2026-11-05)
  - quote-aware comment separation (';' inside '...' or "..." is data)
  - uppercasing outside quotes only (string/char contents preserved)
  - fields: [label] mnemonic [operand]; operand whitespace outside
    quotes is removed
  - formatted format v2: a line without a label starts with one space,
    a line with a label starts at column 0 -- the source's column rule
    is preserved in the formatted tape, so later passes can still
    detect a label from the first byte alone
  - EQU lines are collected (and kept in the output, in order)
  - INCLUDE lines are replaced by the included tape's content;
    per-slot RP preservation on the deck is what makes the
    interleaved reading work, exactly as on the planned firmware

Everything Pass 0 does not understand is counted and listed, so the
report shows where the real source escapes the design assumptions.
"""

from __future__ import annotations

import os
import re
from collections import Counter

import tapedeck as td

# Slot plan (ai-context draft): FLASH shelf 8/9, PSRAM 2 = formatted
SLOT_ASM = 8
SLOT_INC = 9
SLOT_FORMATTED = 2
INCLUDE_MAP = {"BASIC68K.INC": SLOT_INC}

TOKEN_RE = re.compile(r"\S+")


class Stats:
    def __init__(self) -> None:
        self.input_lines = Counter()       # per slot
        self.input_bytes = Counter()
        self.comment_lines = 0
        self.star_comment_lines = 0
        self.blank_lines = 0
        self.output_lines = 0
        self.label_lines = 0
        self.includes = 0
        self.equ = []                      # (name, expr, where)
        self.equ_class = Counter()
        self.equ_other = []                # (name, expr, where)
        self.mnemonics = Counter()         # base mnemonic
        self.mnemonic_suffix = Counter()   # (base, suffix)
        self.labels = {}                   # upper -> set of original spellings
        self.max_label_len = 0
        self.max_line_len = 0
        self.max_operand_len = 0
        self.colon_labels = 0
        self.unterminated_quotes = []      # where
        self.suspicious_col0 = []          # (where, token)
        self.semicolon_in_quotes = 0


class TapeLineReader:
    """Line reader over the deck's streaming API for one slot."""

    def __init__(self, deck: td.TapeDeck, slot: int) -> None:
        self.deck = deck
        self.slot = slot
        self.eof = False
        self.swallow_lf = False

    def read_line(self) -> str | None:
        if self.eof:
            return None
        self.deck.select(self.slot)
        chars = []
        while True:
            b = self.deck.read_byte()
            if b is None:
                self.eof = True
                break
            if b == 0x1A:  # ^Z: end of text (insurance)
                self.eof = True
                break
            if self.swallow_lf:
                self.swallow_lf = False
                if b == 0x0A:
                    continue
            if b == 0x0A:  # LF
                break
            if b == 0x0D:  # CR (or CRLF): line ends, swallow a following LF
                self.swallow_lf = True
                break
            chars.append(b)
        if not chars and self.eof:
            return None
        return bytes(chars).decode("latin-1")


def split_comment(line: str, st: Stats, where: str) -> str:
    """Return the code part before an unquoted ';'."""
    in_s = in_d = False
    for i, ch in enumerate(line):
        if in_s:
            if ch == ";":
                st.semicolon_in_quotes += 1
            elif ch == "'":
                in_s = False
        elif in_d:
            if ch == ";":
                st.semicolon_in_quotes += 1
            elif ch == '"':
                in_d = False
        else:
            if ch == "'":
                in_s = True
            elif ch == '"':
                in_d = True
            elif ch == ";":
                return line[:i]
    if in_s or in_d:
        st.unterminated_quotes.append(where)
    return line


def map_outside_quotes(s: str, fn) -> str:
    out = []
    in_s = in_d = False
    for ch in s:
        if in_s:
            out.append(ch)
            if ch == "'":
                in_s = False
        elif in_d:
            out.append(ch)
            if ch == '"':
                in_d = False
        else:
            if ch == "'":
                in_s = True
                out.append(ch)
            elif ch == '"':
                in_d = True
                out.append(ch)
            else:
                out.append(fn(ch))
    return "".join(out)


def normalize_operand(raw: str) -> str:
    """Uppercase outside quotes, drop whitespace outside quotes."""
    return map_outside_quotes(raw, lambda c: "" if c.isspace() else c.upper())


def classify_equ_expr(e: str) -> str:
    if re.fullmatch(r"\$[0-9A-F]+", e):
        return "const_hex"
    if re.fullmatch(r"[0-9]+", e):
        return "const_dec"
    if re.fullmatch(r"%[01]+", e):
        return "const_bin"
    if re.fullmatch(r"'[^']*'", e):
        return "char_literal"
    if re.fullmatch(r"[A-Z_][A-Z0-9_.$]*", e):
        return "symbol"
    if re.fullmatch(r"[A-Z_][A-Z0-9_.$]*[+-](\$[0-9A-F]+|[0-9]+)", e):
        return "symbol+-const"
    if re.fullmatch(r"[A-Z_][A-Z0-9_.$]*[+-][A-Z_][A-Z0-9_.$]*", e):
        return "symbol+-symbol"
    if re.fullmatch(r"(\$[0-9A-F]+|[0-9]+)[+-](\$[0-9A-F]+|[0-9]+)", e):
        return "const+-const"
    if re.fullmatch(r"(\$[0-9A-F]+|[0-9]+)[+-][A-Z_][A-Z0-9_.$]*", e):
        return "const+-symbol"
    return "OTHER"


DIRECTIVES = {"ORG", "END", "EVEN", "INCLUDE", "EQU", "SET", "DC", "DS"}


class Pass0:
    def __init__(self, deck: td.TapeDeck, out_slot: int) -> None:
        self.deck = deck
        self.out_slot = out_slot
        self.st = Stats()
        # provenance of each formatted line, in output order:
        # (source slot, line number in that file, raw line text)
        self.origins: list[tuple[int, int, str]] = []
        # every raw line read, in encounter order (INCLUDE expansions
        # interleaved): [slot, line number, raw text, formatted index
        # or None when the line produced no formatted line]
        self.source_map: list[list] = []

    def write_out(self, line: str) -> None:
        self.deck.select(self.out_slot)
        for b in line.encode("latin-1"):
            self.deck.write_byte(b)
        self.deck.write_byte(0x0A)
        self.st.output_lines += 1
        self.st.max_line_len = max(self.st.max_line_len, len(line))

    def register_label(self, label_upper: str, original: str) -> None:
        self.st.labels.setdefault(label_upper, set()).add(original)
        self.st.max_label_len = max(self.st.max_label_len, len(label_upper))

    def process_slot(self, slot: int, depth: int) -> None:
        st = self.st
        reader = TapeLineReader(self.deck, slot)
        lineno = 0
        while True:
            raw = reader.read_line()
            if raw is None:
                break
            lineno += 1
            st.input_lines[slot] += 1
            st.input_bytes[slot] += len(raw) + 1
            where = f"slot{slot}:{lineno}"
            map_rec = [slot, lineno, raw, None]
            self.source_map.append(map_rec)
            if raw.strip() == "":
                st.blank_lines += 1
                continue
            if raw[0] == ";":
                st.comment_lines += 1
                continue
            if raw[0] == "*":
                st.star_comment_lines += 1
                continue
            code = split_comment(raw, st, where)
            if code.strip() == "":
                st.comment_lines += 1
                continue
            has_label = not code[0].isspace()
            spans = list(TOKEN_RE.finditer(code))
            tokens = [m.group(0) for m in spans]
            label = mnemonic = None
            operand_raw = ""
            idx = 0
            if has_label:
                label_tok = tokens[0]
                idx = 1
                if label_tok.endswith(":"):
                    st.colon_labels += 1
                    label_tok = label_tok[:-1]
                label = label_tok
                if label_tok.upper() in DIRECTIVES and len(tokens) >= 2:
                    st.suspicious_col0.append((where, label_tok))
                if len(tokens) >= 2:
                    mnemonic = tokens[1]
                    operand_raw = code[spans[1].end():]
            else:
                mnemonic = tokens[0]
                operand_raw = code[spans[0].end():]
            mnemonic_u = mnemonic.upper() if mnemonic else None
            operand = normalize_operand(operand_raw)
            st.max_operand_len = max(st.max_operand_len, len(operand))
            if label:
                st.label_lines += 1
                self.register_label(label.upper(), label)
            if mnemonic_u == "INCLUDE":
                st.includes += 1
                m = re.search(r'"([^"]*)"', operand_raw)
                fname = m.group(1).upper() if m else operand.strip().upper()
                if depth >= 4 or fname not in INCLUDE_MAP:
                    st.equ_other.append(("INCLUDE?", fname, where))
                    continue
                self.process_slot(INCLUDE_MAP[fname], depth + 1)
                continue
            if mnemonic_u:
                base, _, suffix = mnemonic_u.partition(".")
                st.mnemonics[base] += 1
                if suffix:
                    st.mnemonic_suffix[(base, suffix)] += 1
            if mnemonic_u == "EQU" and label:
                st.equ.append((label.upper(), operand, where))
                cls = classify_equ_expr(operand)
                st.equ_class[cls] += 1
                if cls == "OTHER":
                    st.equ_other.append((label.upper(), operand, where))
            parts = []
            if label:
                parts.append(label.upper())
            if mnemonic_u:
                parts.append(mnemonic_u)
            if operand:
                parts.append(operand)
            line_out = " ".join(parts)
            if not label:
                line_out = " " + line_out  # format v2: keep the column rule
            self.origins.append((slot, lineno, raw))
            map_rec[3] = len(self.origins) - 1
            self.write_out(line_out)


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.join(here, "..", "research", "EhBASIC68k-EMU")
    outdir = os.path.join(here, "out")
    os.makedirs(outdir, exist_ok=True)
    deck = td.TapeDeck()
    n_asm = deck.load_file(SLOT_ASM, os.path.join(repo, "basic68k.asm"))
    n_inc = deck.load_file(SLOT_INC, os.path.join(repo, "basic68k.inc"))
    deck.select(SLOT_FORMATTED)
    deck.new_tape()
    p = Pass0(deck, SLOT_FORMATTED)
    p.process_slot(SLOT_ASM, 0)
    st = p.st
    formatted = deck.dump_bytes(SLOT_FORMATTED)
    with open(os.path.join(outdir, "formatted.txt"), "wb") as f:
        f.write(formatted)
    with open(os.path.join(outdir, "equ.txt"), "w") as f:
        for name, expr, _ in st.equ:
            f.write(f"{name} {expr}\n")
    collisions = {k: v for k, v in st.labels.items() if len(v) > 1}
    lines = []
    lines.append("# Pass 0 prototype -- measured results (satoshiokue EhBASIC v3.54)\n")
    lines.append("## Inputs / outputs\n")
    lines.append(f"- basic68k.asm: {n_asm} bytes, {st.input_lines[SLOT_ASM]} lines (slot 8)")
    lines.append(f"- basic68k.inc: {n_inc} bytes, {st.input_lines[SLOT_INC]} lines (slot 9)")
    lines.append(f"- formatted tape: **{len(formatted)} bytes**, {st.output_lines} lines (slot 2)")
    lines.append(f"- formatted BSD sum: {td.format_sum(formatted)}")
    lines.append(f"- lines with label: {st.label_lines}, without label: {st.output_lines - st.label_lines}")
    lines.append(f"- dropped: comment-only {st.comment_lines}, blank {st.blank_lines}, star-comment {st.star_comment_lines}")
    lines.append(f"- INCLUDE expansions: {st.includes}")
    lines.append("\n## EQU\n")
    lines.append(f"- definitions: **{len(st.equ)}**")
    for cls, cnt in st.equ_class.most_common():
        lines.append(f"  - {cls}: {cnt}")
    if st.equ_other:
        lines.append("- expressions outside the (term)+-(term) design:")
        for name, expr, where in st.equ_other:
            lines.append(f"  - {name} = {expr}  ({where})")
    lines.append("\n## Mnemonic census (base, top 40)\n")
    for m, cnt in st.mnemonics.most_common(40):
        lines.append(f"- {m}: {cnt}")
    lines.append(f"\n- distinct base mnemonics: {len(st.mnemonics)}")
    lines.append("\n## Labels\n")
    lines.append(f"- distinct labels (uppercased): {len(st.labels)}")
    lines.append(f"- max label length: {st.max_label_len}")
    lines.append(f"- case-only collisions after uppercasing: {len(collisions)}")
    for k, v in list(collisions.items())[:20]:
        lines.append(f"  - {k}: {sorted(v)}")
    lines.append(f"- labels written with ':': {st.colon_labels}")
    lines.append("\n## Sizes that size the BASIC implementation\n")
    lines.append(f"- max formatted line length: {st.max_line_len}")
    lines.append(f"- max operand length: {st.max_operand_len}")
    lines.append("\n## Anomalies / escapes from design assumptions\n")
    lines.append(f"- unterminated quotes: {len(st.unterminated_quotes)} {st.unterminated_quotes[:10]}")
    lines.append(f"- ';' occurrences inside quotes (quote-aware split mattered): {st.semicolon_in_quotes}")
    lines.append(f"- col-0 directive-like tokens treated as labels: {len(st.suspicious_col0)} {st.suspicious_col0[:10]}")
    report = "\n".join(lines) + "\n"
    with open(os.path.join(outdir, "report.md"), "w") as f:
        f.write(report)
    print(report)


if __name__ == "__main__":
    main()
