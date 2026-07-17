#!/usr/bin/env python3
"""MinerU v4 API client for PDF-to-Markdown extraction.

Uses the MinerU 精准解析 API (v4) with a Bearer token.
Set DEEPPAPERNOTE_MINERU_TOKEN environment variable.

API docs: https://mineru.net/apiManage/docs

Output: *_mineru.md (Markdown), *_mineru.json (manifest)
"""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

MINERU_API_BASE = "https://mineru.net/api/v4/extract/task"
POLL_INTERVAL_SEC = 3
POLL_TIMEOUT_SEC = 600
REQUEST_TIMEOUT = (10, 60)  # (connect, read)


def get_token() -> str:
    token = os.environ.get("DEEPPAPERNOTE_MINERU_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "DEEPPAPERNOTE_MINERU_TOKEN not set. "
            "Create a token at https://mineru.net/apiManage and set the env var."
        )
    return token


def api_request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: int | tuple[int, int] | None = None,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }
    kwargs: dict[str, Any] = {"headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    else:
        kwargs["timeout"] = REQUEST_TIMEOUT

    if json_body is not None:
        kwargs["json"] = json_body

    resp = requests.request(method, url, **kwargs)
    resp.raise_for_status()
    return resp.json()


def submit_task(token: str, pdf_url: str, **kwargs: Any) -> str:
    """Submit a PDF URL for parsing. Returns task_id."""
    body: dict[str, Any] = {
        "url": pdf_url,
        "model_version": "vlm",
        "enable_formula": True,
        "enable_table": True,
        "language": "en",
    }
    body.update({k: v for k, v in kwargs.items() if v is not None})
    result = api_request("POST", MINERU_API_BASE, token, json_body=body)
    if result.get("code") != 0:
        raise RuntimeError(f"MinerU task submission failed: {result.get('msg', result)}")
    task_id = result["data"]["task_id"]
    return task_id


def poll_task(token: str, task_id: str) -> dict[str, Any]:
    """Poll until task completes. Returns the full result data."""
    url = f"{MINERU_API_BASE}/{task_id}"
    deadline = time.time() + POLL_TIMEOUT_SEC
    while time.time() < deadline:
        result = api_request("GET", url, token)
        if result.get("code") != 0:
            raise RuntimeError(f"MinerU poll failed: {result.get('msg', result)}")
        state = result["data"].get("state", "")
        if state == "done":
            return result["data"]
        if state == "failed":
            raise RuntimeError(
                f"MinerU parsing failed: {result['data'].get('err_msg', 'unknown error')}"
            )
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"MinerU task {task_id} timed out after {POLL_TIMEOUT_SEC}s")


def download_and_extract_markdown(full_zip_url: str, output_dir: Path) -> tuple[str, list[str]]:
    """Download the result zip and extract full.md + images with retry.
    Returns (markdown_text, list_of_image_paths)."""
    zip_path = output_dir / "_mineru_result.zip"
    images_dir = output_dir / "images"

    # Retry download up to 3 times
    for attempt in range(3):
        try:
            resp = requests.get(full_zip_url, timeout=(10, 180), stream=True)
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.namelist()
            break
        except (zipfile.BadZipFile, requests.RequestException) as e:
            if attempt == 2:
                raise RuntimeError(f"Failed to download MinerU result after 3 attempts: {e}") from e
            print(f"[mineru] Download attempt {attempt + 1} failed: {e}, retrying...")
            zip_path.unlink(missing_ok=True)
            time.sleep(5)

    image_paths: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        md_name = "full.md"
        if md_name not in zf.namelist():
            md_files = [n for n in zf.namelist() if n.endswith(".md")]
            md_name = md_files[0] if md_files else None
        if md_name is None:
            raise RuntimeError("No .md file found in MinerU result zip")
        md_text = zf.read(md_name).decode("utf-8")

        # Extract images
        images_dir.mkdir(parents=True, exist_ok=True)
        for name in zf.namelist():
            if name.startswith("images/") and not name.endswith("/"):
                zf.extract(name, output_dir)
                image_paths.append(str(output_dir / name))

    zip_path.unlink(missing_ok=True)
    return md_text, image_paths


def split_markdown_sections(md_text: str) -> list[dict[str, Any]]:
    """Split markdown into sections matching the raw_sections.jsonl format."""
    import re as _re

    sections: list[dict[str, Any]] = []
    current_title = "preamble"
    current_lines: list[str] = []
    seen: dict[str, int] = {}

    def flush_section() -> None:
        nonlocal current_lines
        text = "\n".join(current_lines).strip()
        if text:
            section_id_base = _re.sub(r"[^a-z0-9]+", "-", current_title.lower()).strip("-") or "section"
            seen[section_id_base] = seen.get(section_id_base, 0) + 1
            suffix = "" if seen[section_id_base] == 1 else f"-{seen[section_id_base]}"
            sections.append({
                "record_type": "section",
                "section_id": f"sec:{section_id_base}{suffix}",
                "kind": "section",
                "title": current_title,
                "page_start": 0,
                "page_end": 0,
                "text": text,
                "char_count": len(text),
                "text_hash_sha256": __import__("hashlib").sha256(
                    text.encode("utf-8")
                ).hexdigest(),
            })
        current_lines = []

    for raw_line in md_text.splitlines():
        line = raw_line.strip()
        heading_match = _re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading_match:
            flush_section()
            current_title = heading_match.group(2).strip()
            continue
        if line:
            current_lines.append(line)

    flush_section()
    return sections


def run(pdf_url: str, output_dir: str, **kwargs: Any) -> dict[str, Any]:
    """Run the full MinerU extraction pipeline. Returns a manifest dict."""
    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    token = get_token()
    print(f"[mineru] Submitting: {pdf_url[:80]}...")
    task_id = submit_task(token, pdf_url, **kwargs)
    print(f"[mineru] Task ID: {task_id}, polling...")
    result = poll_task(token, task_id)

    full_zip_url = result.get("full_zip_url", "")
    if not full_zip_url:
        raise RuntimeError("MinerU returned no full_zip_url")

    print(f"[mineru] Downloading result...")
    md_text, image_paths = download_and_extract_markdown(full_zip_url, out)

    # Repair MinerU LaTeX for Obsidian compatibility
    from repair_mineru_latex import merge_adjacent_math, fix_array_display, escape_html_tokens
    md_text = merge_adjacent_math(md_text)
    md_text = fix_array_display(md_text)
    md_text = escape_html_tokens(md_text)

    # Save markdown
    md_path = out / "_mineru_full.md"
    md_path.write_text(md_text, encoding="utf-8")
    print(f"[mineru] Saved markdown: {md_path} ({len(md_text)} chars, {len(image_paths)} images)")

    # Split into sections
    sections = split_markdown_sections(md_text)
    print(f"[mineru] Sections: {len(sections)}")

    return {
        "status": "ok",
        "script": "mineru_client.py",
        "task_id": task_id,
        "markdown_path": str(md_path),
        "image_paths": image_paths,
        "sections": sections,
        "total_chars": len(md_text),
        "section_count": len(sections),
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__ or "MinerU API client")
    p.add_argument("--pdf-url", required=True, help="URL of the PDF to parse.")
    p.add_argument("--output-dir", default=".", help="Directory for output files.")
    p.add_argument("--language", default="en", help="Document language (default: en).")
    return p


def main() -> None:
    args = parser().parse_args()
    try:
        manifest = run(args.pdf_url, args.output_dir, language=args.language)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
