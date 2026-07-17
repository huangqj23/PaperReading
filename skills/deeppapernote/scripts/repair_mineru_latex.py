#!/usr/bin/env python3
"""Repair MinerU-extracted LaTeX in Markdown for better Obsidian/MathJax rendering.

Fixes:
  1. Merges adjacent $...=$ $...$ blocks MinerU incorrectly split
  2. Wraps array environment in display math $$...$$
  3. Escapes HTML-like tokens (<null>, <box>, </box>) with backticks
"""

import re
from pathlib import Path


def merge_adjacent_math(text: str) -> str:
    """Merge $...=$ $...$ into a single $... = ...$ expression."""
    pattern = re.compile(
        r'\$\s*([^$]+?)\s*=\s*\$'  # $...=$  (captures before =)
        r'\s*'                       # whitespace
        r'\$\s*([^$]+?)\s*\$',       # $...$   (captures second part)
    )

    def _merge(m: re.Match) -> str:
        part1 = m.group(1).strip()
        part2 = m.group(2).strip()
        if part1 and part2:
            return f"${part1} = {part2}$"
        return m.group(0)

    return pattern.sub(_merge, text)


def fix_array_display(text: str) -> str:
    """Wrap array/align environments in $$ instead of $."""
    pattern = re.compile(r'\$(\\begin\{array\}[\s\S]*?\\end\{array\})\$')
    return pattern.sub(r'$$\n\1\n$$', text)


def escape_html_tokens(text: str) -> str:
    """Backtick-escape tokens that look like HTML tags."""
    for token in ['<null>', '<box>', '</box>', '</ref>']:
        text = text.replace(token, f'`{token}`')
    return text


def repair_markdown(input_path: str, output_path: str | None = None) -> str:
    path = Path(input_path)
    text = path.read_text(encoding='utf-8')
    original_len = len(text)

    text = merge_adjacent_math(text)
    text = fix_array_display(text)
    text = escape_html_tokens(text)

    out = output_path or str(path.with_stem(path.stem + '_fixed'))
    Path(out).write_text(text, encoding='utf-8')
    print(f"[repair] {path.name}: {original_len} -> {len(text)} chars, saved to {out}")
    return text


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Repair MinerU LaTeX in Markdown.')
    p.add_argument('input', help='Input Markdown file')
    p.add_argument('--output', '-o', help='Output path (default: input_fixed.md)')
    args = p.parse_args()
    repair_markdown(args.input, args.output)
