#!/usr/bin/env python3
"""Install, change, or remove Web Search's managed Hyprland binding.

The widget's global shortcut lives in ~/.config/hypr/bindings.lua inside a
clearly marked managed block. This helper is the supported way to change it:
it backs the file up, refuses to steal a key that is already bound elsewhere,
writes the block, records the key in the widget's shell.json setting (so the
panel legend matches), and reloads Hyprland so the new binding is live.

Examples:
  python3 scripts/bindings.py set "SUPER + CTRL + K"
  python3 scripts/bindings.py install
  python3 scripts/bindings.py remove
  python3 scripts/bindings.py status
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


PLUGIN_ID = "io.github.sahzudin.omarchy-google-search"
DEFAULT_KEY = "SUPER + ALT + P"
DEFAULT_KEYCAP = "Super+Alt+P"
BEGIN = "-- BEGIN Web Search managed binding"
END = "-- END Web Search managed binding"
ACTION = f"omarchy-shell shell toggle {PLUGIN_ID} {{}}"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:\s*\+\s*[A-Za-z0-9_-]+)*$")


def binding_file() -> Path:
    return Path.home() / ".config" / "hypr" / "bindings.lua"


def block_for(key: str) -> str:
    return f"{BEGIN}\no.bind(\"{key}\", \"Web search\", \"{ACTION}\")\n{END}\n"


def validate_key(key: str) -> str:
    key = " + ".join(part.strip().upper() for part in key.split("+"))
    if not KEY_PATTERN.fullmatch(key):
        raise RuntimeError(
            "invalid key combination; use names joined by '+', for example SUPER + CTRL + K"
        )
    return key


def installed_block(text: str) -> str | None:
    start = text.find(BEGIN)
    if start < 0:
        return None
    end = text.find(END, start)
    if end < 0:
        raise RuntimeError(f"found {BEGIN!r} without its closing marker")
    return text[start : end + len(END)]


def remove_block(text: str) -> tuple[str, bool]:
    start = text.find(BEGIN)
    if start < 0:
        return text, False
    end = text.find(END, start)
    if end < 0:
        raise RuntimeError(f"found {BEGIN!r} without its closing marker")
    end += len(END)
    if end < len(text) and text[end] == "\n":
        end += 1
    if start > 0 and text[start - 1] == "\n" and end == len(text):
        start -= 1
    return text[:start] + text[end:], True


def current_key(text: str) -> str | None:
    block = installed_block(text)
    if not block:
        return None
    # Pull the key out of o.bind("<KEY>", "Web search", ...).
    inner = block.split('o.bind("', 1)[1]
    key = inner.split('",', 1)[0]
    return key


def key_is_free(key: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["omarchy", "menu", "keybindings", "--print"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as error:
        raise RuntimeError("omarchy is required to check whether the key is free") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("timed out while checking existing Omarchy keybindings") from error

    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"could not check existing Omarchy keybindings: {detail}")

    wanted = normalize_key(key)
    for line in result.stdout.splitlines():
        left, separator, right = line.partition("→")
        if separator and normalize_key(left) == wanted:
            return False, right.strip()
    return True, ""


def normalize_key(key: str) -> str:
    return " ".join(str(key).replace("+", " + ").split()).upper()


def persist_setting(key: str) -> None:
    try:
        subprocess.run(
            ["omarchy", "bar", "set", PLUGIN_ID, "openShortcut", key],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"bindings.py: could not update shell.json setting: {error}", file=sys.stderr)


def reload_hyprland() -> None:
    try:
        subprocess.run(["hyprctl", "reload"], check=False, capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    backup_path = path.with_name(f"{path.name}.bak.omarchy-search-{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def cmd_set(path: Path, key: str) -> int:
    key = validate_key(key)
    if not path.exists():
        raise RuntimeError(f"{path} does not exist")
    text = path.read_text(encoding="utf-8")
    new_block = block_for(key)

    if installed_block(text) == new_block:
        print(f"Keybinding already installed: {key}")
        return 0

    # Strip our previous block (if any) before checking/rewriting.
    text, owned = remove_block(text)

    if path == binding_file():
        free, description = key_is_free(key)
        if not free:
            print(f"Skipping {key}; it is already bound to {description}.", file=sys.stderr)
            print(f"Add this command to a key of your choice: {ACTION}", file=sys.stderr)
            return 0

    if owned:
        backup_path = backup(path)
        print(f"Replacing previous Web Search keybinding (backup: {backup_path})")
    else:
        backup_path = backup(path)
        print(f"Installing {key} keybinding (backup: {backup_path})")

    if not text or text.endswith("\n\n"):
        separator = ""
    elif text.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    path.write_text(text + separator + new_block, encoding="utf-8")
    # Only touch the live config (setting + reload) when this is the real file;
    # --file is for tests and must stay side-effect free.
    if path == binding_file():
        persist_setting(key)
        reload_hyprland()
    else:
        print("(dry run: live shell.json setting and Hyprland not touched)")
    print(f"Active keybinding: {key}")
    return 0


def cmd_install(path: Path) -> int:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    key = current_key(text) or DEFAULT_KEY
    return cmd_set(path, key)


def cmd_remove(path: Path) -> int:
    if not path.exists():
        print("Hyprland bindings file is absent; nothing to remove.")
        return 0
    text = path.read_text(encoding="utf-8")
    updated, changed = remove_block(text)
    if not changed:
        print("No managed Web Search keybinding found.")
        return 0
    backup_path = backup(path)
    path.write_text(updated, encoding="utf-8")
    if path == binding_file():
        reload_hyprland()
    print(f"Removed Web Search keybinding (backup: {backup_path}).")
    return 0


def cmd_status(path: Path) -> int:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    key = current_key(text)
    if key:
        print(f"Managed Web Search keybinding: {key}")
    else:
        print("No managed Web Search keybinding installed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("set", "install", "remove", "status"))
    parser.add_argument("key", nargs="?", default=None, help="key combination, e.g. \"SUPER + CTRL + K\"")
    parser.add_argument("--key", dest="key_opt", default=None, help="same as the positional KEY")
    parser.add_argument("--file", type=Path, default=binding_file(), help="bindings.lua path (for tests)")
    arguments = parser.parse_args()

    key = arguments.key_opt or arguments.key
    try:
        if arguments.action == "set":
            if not key:
                raise RuntimeError("set requires a key, e.g. set \"SUPER + CTRL + K\"")
            return cmd_set(arguments.file, key)
        if arguments.action == "install":
            return cmd_install(arguments.file)
        if arguments.action == "remove":
            return cmd_remove(arguments.file)
        return cmd_status(arguments.file)
    except (OSError, RuntimeError) as error:
        print(f"bindings.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
