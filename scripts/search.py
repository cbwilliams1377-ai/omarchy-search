#!/usr/bin/env python3
"""Attach a clipboard image to a Web Search query and run the search.

The bar panel is limited to opening URLs, but reverse image search and vision
chat prompts need a public URL for the image. This helper fills the gap:

  attach  Resolve the current clipboard image to a temp file and print its
          path (one line on stdout, exit 0). Exits 1 when the clipboard does
          not hold an image.
  search  Upload an attached image to a temporary image host and open the
          results in the default browser for the selected engine.

Engine behavior with an image attached:
  google         Google Lens reverse image search (search-by-URL).
  bing           Bing Visual Search (search-by-URL).
  chatgpt/claude The image URL is embedded into the prefilled prompt, so the
                 chat can see it next to the typed text.
  other engines  Safe text-only fallback; the image cannot be used there.

Hosts (a failing host falls back to the next in the chain):
  catbox     Persistent links (files.catbox.moe); permissive to AI fetchers.
  litterbox  Auto-deleted after 1 hour; ideal when only the user's browser
             reads the URL (Google/Bing reverse search).
  0x0        Expires after ~30 days; privacy-friendly middle ground.

With --host auto (the default) ChatGPT/Claude images use catbox first, because
Litterbox's anti-bot rules return 403 to their image fetchers; Google/Bing use
litterbox first since the URL is opened by the user's own browser.

No shell strings are evaluated: commands are argument arrays and queries are
URL-encoded before they reach the browser.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
CATBOX_API = "https://catbox.moe/user/api.php"
ZER0X_API = "https://0x0.st/"
USER_AGENT = "omarchy-web-search/1.3"
OMARCHY_HISTORY = Path.home() / ".local" / "state" / "omarchy" / "clipboard-history.json"

HOST_LABELS = {
    "catbox": "catbox (persistent)",
    "litterbox": "litterbox (expires in 1h)",
    "0x0": "0x0.st (expires in ~30d)",
}

HOST_ENDPOINTS = {
    "catbox": CATBOX_API,
    "litterbox": LITTERBOX_API,
    "0x0": ZER0X_API,
}

# Multipart form fields per host; "@{path}" expands to "value@<image>" in the
# single "-F name=value" argument curl expects.
HOST_FORM_FIELDS = {
    "catbox": ["reqtype=fileupload", "fileToUpload=@{path}"],
    "litterbox": ["reqtype=fileupload", "time=1h", "fileToUpload=@{path}"],
    "0x0": ["file=@{path}"],
}

# Preferred hosts when the plugin setting is "auto": prompt engines upload for
# AI fetchers (Litterbox 403s them), reverse-search URLs are read by the user's
# own browser so short-lived hosts are fine.
AUTO_HOST_CHAINS = {
    "prompt": ["catbox", "0x0", "litterbox"],
    "lens": ["litterbox", "catbox", "0x0"],
    "bing": ["litterbox", "catbox", "0x0"],
    "none": ["litterbox", "catbox", "0x0"],
}

TEXT_PREFIXES = {
    "google": "https://www.google.com/search?q=",
    "chatgpt": "https://chatgpt.com/?q=",
    "claude": "https://claude.ai/new?q=",
    "bing": "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "github": "https://github.com/search?q=",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
    "youtube": "https://www.youtube.com/results?search_query=",
    "reddit": "https://www.reddit.com/search/?q=",
    "stackoverflow": "https://stackoverflow.com/search?q=",
}

# How each engine consumes an attached image: "lens"/"bing" reverse-search by
# URL, "prompt" hands the image to a vision chat via the prefilled prompt, and
# "none" falls back to a plain text search.
IMAGE_KINDS = {
    "google": "lens",
    "bing": "bing",
    "chatgpt": "prompt",
    "claude": "prompt",
}


class UploadError(RuntimeError):
    """Raised when the image could not be uploaded to a temporary host."""


def text_url(engine_id: str, query: str) -> str:
    return TEXT_PREFIXES[engine_id] + quote(query, safe="")


def lens_url(image_url: str) -> str:
    return "https://lens.google.com/uploadbyurl?url=" + quote(image_url, safe="")


def bing_visual_url(image_url: str) -> str:
    encoded = quote(image_url, safe="")
    return (
        "https://www.bing.com/images/search?view=detailv2&iss=sbi&form=SBIHMP"
        f"&sbisrc=UrlPaste&q=imgurl:{encoded}"
    )


def prompt_for_image(engine_id: str, query: str, image_url: str) -> str:
    """Build a prefilled chat prompt that carries the image next to the text."""
    if engine_id == "claude":
        image_markdown = f"![image]({image_url})"
        return image_markdown if not query else f"{image_markdown}\n{query}"
    if not query:
        return image_url
    return f"{query}\n{image_url}"


def search_url(engine_id: str, query: str, image_url: str) -> str:
    kind = IMAGE_KINDS.get(engine_id, "none")
    if kind == "lens":
        return lens_url(image_url)
    if kind == "bing":
        return bing_visual_url(image_url)
    if kind == "prompt":
        return TEXT_PREFIXES[engine_id] + quote(
            prompt_for_image(engine_id, query, image_url), safe=""
        )
    return text_url(engine_id, query) if query else image_url


def host_chain(kind: str, host: str | None) -> list[str]:
    """Ordered list of hosts to try, honoring the --host/auto preference."""
    if host in HOST_ENDPOINTS:
        return [host] + [candidate for candidate in HOST_ENDPOINTS if candidate != host]
    return list(AUTO_HOST_CHAINS.get(kind, AUTO_HOST_CHAINS["none"]))


def _curl_args(path: Path, host: str, timeout: int) -> list[str]:
    arguments = ["curl", "-sS", "-A", USER_AGENT, "--fail", "--max-time", str(timeout)]
    for field in HOST_FORM_FIELDS[host]:
        arguments += ["-F", field.format(path=path)]
    arguments.append(HOST_ENDPOINTS[host])
    return arguments


def upload_image(path: Path, kind: str = "prompt", host: str = "auto", timeout: int = 60) -> str:
    """Upload to the first host that accepts the file and return its URL."""
    last_detail = ""
    for host_id in host_chain(kind, host):
        for _attempt in range(2):
            try:
                result = subprocess.run(
                    _curl_args(path, host_id, timeout),
                    capture_output=True,
                    text=True,
                    timeout=timeout + 10,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                last_detail = str(error)
                continue
            if result.returncode == 0:
                url = result.stdout.strip()
                if url.startswith("http://") or url.startswith("https://"):
                    return url
                last_detail = f"{HOST_LABELS[host_id]} returned: {url}"
            else:
                last_detail = (
                    result.stderr.strip() or f"curl exit {result.returncode}"
                ).splitlines()[-1]
    raise UploadError(last_detail)


def launch(url: str) -> None:
    try:
        subprocess.run(
            ["omarchy", "launch", "browser", url],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        print(f"search.py: could not open browser: {error}", file=sys.stderr)


def cmd_search(engine_id: str, query: str, image_path: str | None, host: str = "auto") -> int:
    if engine_id not in TEXT_PREFIXES:
        print(f"search.py: unknown engine {engine_id!r}", file=sys.stderr)
        return 2
    if host != "auto" and host not in HOST_ENDPOINTS:
        print(f"search.py: unknown image host {host!r} (auto|{'|'.join(HOST_ENDPOINTS)})", file=sys.stderr)
        return 2

    kind = IMAGE_KINDS.get(engine_id, "none")
    if image_path and os.path.isfile(image_path):
        try:
            image_url = upload_image(Path(image_path), kind=kind, host=host)
        except UploadError as error:
            print(
                f"search.py: image upload failed ({error}); "
                "falling back to a text-only search",
                file=sys.stderr,
            )
            image_url = ""
        if image_url:
            launch(search_url(engine_id, query, image_url))
            return 0
        if query:
            launch(text_url(engine_id, query))
            return 0
        # Upload failed and there is no text: show the image so it isn't
        # silently dropped.
        launch("file://" + image_path)
        return 0

    if query:
        launch(text_url(engine_id, query))
        return 0
    print("search.py: nothing to search", file=sys.stderr)
    return 1


def clipboard_image_data() -> bytes | None:
    try:
        listing = subprocess.run(
            ["timeout", "3", "wl-paste", "--list-types"],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if listing.returncode != 0:
        return None
    if not any("image/" in kind.strip() for kind in listing.stdout.splitlines()):
        return None

    try:
        data = subprocess.run(
            ["timeout", "3", "wl-paste", "--type", "image/png"],
            capture_output=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if data.returncode != 0 or not data.stdout:
        return None
    return data.stdout


def history_image_path() -> Path | None:
    try:
        entries = json.loads(OMARCHY_HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("type") != "image":
            continue
        path = Path(entry.get("path") or "")
        if path.is_file():
            return path
    return None


def cmd_attach() -> int:
    data = clipboard_image_data()
    if data is not None:
        try:
            destination = Path(tempfile.gettempdir()) / f"omarchy-web-search-{secrets.token_hex(6)}.png"
            destination.write_bytes(data)
        except OSError as error:
            print(f"search.py: could not write clipboard image: {error}", file=sys.stderr)
            return 2
        print(destination)
        return 0

    source = history_image_path()
    if source is not None:
        try:
            destination = Path(tempfile.gettempdir()) / f"omarchy-web-search-{secrets.token_hex(6)}.png"
            shutil.copyfile(source, destination)
        except OSError as error:
            print(f"search.py: could not copy clipboard image: {error}", file=sys.stderr)
            return 2
        print(destination)
        return 0

    print("search.py: no image on the clipboard", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("attach", help="resolve the current clipboard image")

    search_parser = subparsers.add_parser("search", help="run a text and/or image search")
    search_parser.add_argument("--engine", required=True, help="engine id from the panel engine list")
    search_parser.add_argument("--query", default="", help="typed query text")
    search_parser.add_argument("--image", default=None, help="path to an attached image")
    search_parser.add_argument(
        "--host",
        default="auto",
        help="image host preference: auto, catbox, litterbox, or 0x0",
    )

    arguments = parser.parse_args()
    if arguments.action == "attach":
        return cmd_attach()
    return cmd_search(arguments.engine, arguments.query, arguments.image, arguments.host)


if __name__ == "__main__":
    raise SystemExit(main())