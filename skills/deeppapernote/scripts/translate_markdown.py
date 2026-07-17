#!/usr/bin/env python3
"""Translate a Markdown file to Chinese while preserving structure.

Uses an OpenAI-compatible LLM API. Configure via environment variables:
  DEEPPAPERNOTE_TRANSLATE_BASE_URL  (default: https://api.deepseek.com/v1)
  DEEPPAPERNOTE_TRANSLATE_KEY       (default: $DEEPPAPERNOTE_TRANSLATE_KEY)
  DEEPPAPERNOTE_TRANSLATE_MODEL     (default: deepseek-chat)

Preserves: code blocks, math formulas ($/$$), image refs, HTML tables, links.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

# ── config ────────────────────────────────────────────────────────────
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-pro"
MAX_CHUNK_CHARS = 6000
CHUNK_OVERLAP = 200


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def translate_config() -> dict[str, str]:
    base = _env("DEEPPAPERNOTE_TRANSLATE_BASE_URL", DEFAULT_BASE_URL)
    key = _env("DEEPPAPERNOTE_TRANSLATE_KEY")
    model = _env("DEEPPAPERNOTE_TRANSLATE_MODEL", DEFAULT_MODEL)
    if not key:
        raise SystemExit(
            "No translation API key found. Set DEEPPAPERNOTE_TRANSLATE_KEY."
        )
    return {"base_url": base, "api_key": key, "model": model}


# ── markdown splitting ────────────────────────────────────────────────
def split_by_headings(md_text: str) -> list[dict[str, Any]]:
    """Split markdown into chunks at ## headings, keeping # title as first chunk."""
    lines = md_text.splitlines()
    chunks: list[dict[str, Any]] = []
    current_lines: list[str] = []
    current_heading = ""

    for line in lines:
        if re.match(r"^##\s", line):
            if current_lines:
                chunks.append({"heading": current_heading, "text": "\n".join(current_lines).strip()})
            current_heading = line
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        chunks.append({"heading": current_heading, "text": "\n".join(current_lines).strip()})

    return chunks


# ── protection ────────────────────────────────────────────────────────
PLACEHOLDER_MAP: dict[str, str] = {}
_placeholder_counter = 0


def _protect(text: str, pattern: str, prefix: str) -> str:
    global _placeholder_counter
    result = text
    for match in re.finditer(pattern, result, re.MULTILINE | re.DOTALL):
        key = f"__{prefix}_{_placeholder_counter}__"
        _placeholder_counter += 1
        PLACEHOLDER_MAP[key] = match.group(0)
        result = result.replace(match.group(0), key)
    return result


def protect_specials(text: str) -> str:
    """Replace code blocks, math, images, links, tables with placeholders."""
    global _placeholder_counter
    PLACEHOLDER_MAP.clear()
    _placeholder_counter = 0

    t = _protect(text, r"```[\s\S]*?```", "CODE")
    t = _protect(t, r"\$\$[\s\S]*?\$\$", "MATHBLOCK")
    t = _protect(t, r"\$[^\$\n]+?\$", "MATH")
    t = _protect(t, r"!\[.*?\]\(.*?\)", "IMG")
    t = _protect(t, r"\[([^\]]*)\]\(([^\)]*)\)", "LINK")
    t = _protect(t, r"<table[\s\S]*?</table>", "TABLE")
    return t


def restore_specials(text: str) -> str:
    for key, value in PLACEHOLDER_MAP.items():
        text = text.replace(key, value)
    return text


# ── API call ──────────────────────────────────────────────────────────
def translate_chunk(chunk_text: str, config: dict[str, str]) -> str:
    """Translate a chunk of markdown to Chinese."""
    system_prompt = (
        "You are a professional academic translator. "
        "Translate the following academic paper content from English to Chinese. "
        "RULES:\n"
        "- Translate ALL English text to natural, fluent Chinese suitable for academic reading.\n"
        "- Preserve ALL Markdown formatting: headings (#, ##, ###), bold (**), italic (*), lists.\n"
        "- Preserve ALL placeholders exactly as-is (e.g. __CODE_0__, __MATH_0__, __IMG_0__, __TABLE_0__).\n"
        "- Keep proper nouns (model names, dataset names, author names) in their original form or use widely-accepted Chinese translations.\n"
        "- Keep numeric values, percentages, and units unchanged.\n"
        "- Output ONLY the translated text, no explanations."
    )

    url = f"{config['base_url'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": chunk_text},
        ],
        "temperature": 0.1,
    }

    for attempt in range(3):
        try:
            # Use a session with HTTP/1.1 keep-alive to avoid chunked encoding issues
            session = requests.Session()
            session.mount("https://", requests.adapters.HTTPAdapter(
                max_retries=3,
                pool_connections=1,
                pool_maxsize=1,
            ))
            resp = session.post(
                url, headers=headers, json=payload,
                timeout=(15, 180),
                stream=False,
            )
            resp.raise_for_status()
            data = resp.json()
            session.close()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"Translation failed after 3 attempts: {e}") from e
            wait = (attempt + 1) * 8
            print(f"  [translate] attempt {attempt + 1} failed: {e}, retrying in {wait}s...")
            time.sleep(wait)

    return chunk_text  # unreachable


# ── main pipeline ─────────────────────────────────────────────────────
def translate_markdown(
    md_text: str,
    config: dict[str, str],
    *,
    output_path: str | None = None,
) -> str:
    """Translate a full Markdown document to Chinese. Returns translated text."""
    chunks = split_by_headings(md_text)
    translated_chunks: list[str] = []

    for i, chunk in enumerate(chunks):
        heading = chunk["heading"].strip()
        text = chunk["text"]

        # Skip very short chunks (references mostly)
        if len(text) < 100 and i > len(chunks) * 0.8:
            translated_chunks.append(text)
            if heading:
                print(f"  [{i+1}/{len(chunks)}] {heading[:60]} (skipped, too short)")
            continue

        # Protect special elements
        protected = protect_specials(text)

        # If chunk is small enough, translate directly
        if len(protected) <= MAX_CHUNK_CHARS:
            print(f"  [{i+1}/{len(chunks)}] {heading[:60]} ({len(protected)} chars)")
            translated = translate_chunk(protected, config)
            translated = restore_specials(translated)
        else:
            # Split large chunks by paragraphs
            print(f"  [{i+1}/{len(chunks)}] {heading[:60]} ({len(protected)} chars, split)")
            paragraphs = re.split(r"\n\n+", protected)
            sub_translated: list[str] = []
            buffer = ""
            for para in paragraphs:
                if len(buffer) + len(para) > MAX_CHUNK_CHARS and buffer:
                    sub_translated.append(translate_chunk(buffer, config))
                    buffer = para
                else:
                    buffer = (buffer + "\n\n" + para).strip()
            if buffer:
                sub_translated.append(translate_chunk(buffer, config))
            translated = restore_specials("\n\n".join(sub_translated))

        translated_chunks.append(translated)

    result = "\n\n".join(translated_chunks)
    # Repair any broken MinerU LaTeX in the restored math blocks
    from repair_mineru_latex import merge_adjacent_math, fix_array_display, escape_html_tokens
    result = merge_adjacent_math(result)
    result = fix_array_display(result)
    result = escape_html_tokens(result)
    if output_path:
        Path(output_path).write_text(result, encoding="utf-8")
        print(f"[translate] saved to {output_path}")
    return result


# ── CLI ───────────────────────────────────────────────────────────────
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Translate Markdown to Chinese via LLM API.")
    p.add_argument("--input", required=True, help="Input Markdown file path.")
    p.add_argument("--output", required=True, help="Output translated Markdown file path.")
    return p


def main() -> None:
    args = parser().parse_args()
    md_text = Path(args.input).read_text(encoding="utf-8")
    config = translate_config()
    print(f"[translate] model={config['model']}, input={len(md_text)} chars")
    translate_markdown(md_text, config, output_path=args.output)


if __name__ == "__main__":
    main()
