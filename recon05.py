"""Recon for Pass 0.5: operand-shape census over the formatted tape.

Reads out/formatted.txt (format v2: label lines start at column 0,
label-less lines start with one space) and classifies every operand
of every instruction line, plus the items of DC/DS lines, so the
intermediate record layout is designed from measured shapes.
"""

from __future__ import annotations

import os
import re
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))

TERM = r"(?:\$[0-9A-F]+|[0-9]+|'[^']*'|[A-Z_][A-Z0-9_.$]*|\*)"
EXPR = re.compile(rf"^[+-]?{TERM}(?:[+-]{TERM})?$")
REGLIST = re.compile(r"^[DA][0-7](?:-[DA][0-7])?(?:/[DA][0-7](?:-[DA][0-7])?)*$")


def split_operands(operand: str) -> list[str]:
    """Split at top-level commas (outside quotes and parentheses)."""
    parts, depth, in_s, in_d, cur = [], 0, False, False, []
    for ch in operand:
        if in_s:
            cur.append(ch)
            if ch == "'":
                in_s = False
        elif in_d:
            cur.append(ch)
            if ch == '"':
                in_d = False
        elif ch == "'":
            in_s = True
            cur.append(ch)
        elif ch == '"':
            in_d = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def classify(op: str) -> tuple[str, str]:
    """Return (shape, detail). Shapes are candidate operand type codes."""
    if op == "":
        return ("NONE", "")
    if re.fullmatch(r"[DA][0-7]", op):
        return ("REG", op)
    if op == "SP":
        return ("REG", "A7")
    if op in ("SR", "CCR", "USP"):
        return ("SPECIAL", op)
    if REGLIST.fullmatch(op):
        return ("REGLIST", "")
    m = re.fullmatch(r"#(.*)", op)
    if m:
        return ("IMM", "expr-ok" if EXPR.fullmatch(m.group(1)) else "expr-BAD:" + m.group(1))
    rt = r"(?:[DA][0-7]|SP)"
    m = re.fullmatch(rf"\(({rt})\)\+", op)
    if m:
        return ("POSTINC", m.group(1))
    m = re.fullmatch(rf"-\(({rt})\)", op)
    if m:
        return ("PREDEC", m.group(1))
    m = re.fullmatch(rf"\(({rt})\)", op)
    if m:
        return ("IND", m.group(1))
    m = re.fullmatch(r"(.*)\(PC,([DA][0-7])(?:\.([WL]))?\)", op)
    if m:
        return ("INDEXPC", "expr-ok" if m.group(1) in ("", "0") or EXPR.fullmatch(m.group(1)) else "expr-BAD:" + m.group(1))
    m = re.fullmatch(rf"(.*)\(({rt}),([DA][0-7])(?:\.([WL]))?\)", op)
    if m:
        return ("INDEX", "expr-ok" if m.group(1) in ("", "0") or EXPR.fullmatch(m.group(1)) else "expr-BAD:" + m.group(1))
    m = re.fullmatch(r"(.*)\(PC\)", op)
    if m:
        return ("DISPPC", "expr-ok" if EXPR.fullmatch(m.group(1)) else "expr-BAD:" + m.group(1))
    m = re.fullmatch(rf"(.*)\(({rt})\)", op)
    if m:
        return ("DISP", "expr-ok" if EXPR.fullmatch(m.group(1)) else "expr-BAD:" + m.group(1))
    if EXPR.fullmatch(op):
        return ("ADDR", "")
    return ("OTHER", op)


def main() -> None:
    with open(os.path.join(HERE, "out", "formatted.txt"), "rb") as f:
        text = f.read().decode("latin-1")
    shapes = Counter()
    other_examples = []
    bad_expr = []
    bad_total = Counter()
    dc_bad = []
    operand_counts = Counter()
    dc_items = Counter()
    dc_string_lens = []
    dc_total_items = 0
    lines = text.split("\n")
    for line in lines:
        if not line:
            continue
        has_label = line[0] != " "
        body = line[1:] if not has_label else line
        fields = body.split(" ", 2) if has_label else body.split(" ", 1)
        if has_label:
            mnemonic = fields[1] if len(fields) > 1 else ""
            operand = fields[2] if len(fields) > 2 else ""
        else:
            mnemonic = fields[0]
            operand = fields[1] if len(fields) > 1 else ""
        base = mnemonic.partition(".")[0]
        if base in ("DC", "DS"):
            items = split_operands(operand) if operand else []
            dc_items[len(items)] += 1
            dc_total_items += len(items)
            for it in items:
                if it.startswith('"') and it.endswith('"'):
                    dc_string_lens.append(len(it) - 2)
                elif it.startswith("'"):
                    pass  # char literal item
                elif not EXPR.fullmatch(it):
                    dc_bad.append((line, it))
            continue
        ops = split_operands(operand) if operand else []
        operand_counts[len(ops)] += 1
        for op in ops:
            shape, detail = classify(op)
            shapes[shape] += 1
            if shape == "OTHER" and len(other_examples) < 60:
                other_examples.append((line, op))
            if "BAD" in detail:
                bad_total[shape] += 1
                if len(bad_expr) < 60:
                    bad_expr.append((line, op, detail))
    print("operand count per instruction line:", dict(sorted(operand_counts.items())))
    print("operand shapes:", dict(shapes.most_common()))
    print("DC/DS items per line:", dict(sorted(dc_items.items())), "total items:", dc_total_items)
    if dc_string_lens:
        print("DC string items:", len(dc_string_lens), "max string len:", max(dc_string_lens))
    print("\nOTHER operands (line // operand):")
    for line, op in other_examples:
        print(" ", line, "//", op)
    print("\nBAD expressions total by shape:", dict(bad_total))
    for line, op, d in bad_expr:
        print(" ", line, "//", op, "//", d)
    print("\nBAD DC/DS items:")
    for line, it in dc_bad:
        print(" ", line, "//", it)


if __name__ == "__main__":
    main()
