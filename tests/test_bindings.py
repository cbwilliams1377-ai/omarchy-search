from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import bindings


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bindings.py"
BEGIN = "-- BEGIN Web Search managed binding"
END = "-- END Web Search managed binding"


class BindingsCliTests(unittest.TestCase):
    def run_helper(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_set_rejects_symlink_without_changing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim.lua"
            victim.write_text("-- victim\n", encoding="utf-8")
            binding = root / "bindings.lua"
            binding.symlink_to(victim)

            result = self.run_helper(
                "set", "SUPER + CTRL + K", "--file", str(binding)
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(victim.read_text(encoding="utf-8"), "-- victim\n")

    def test_install_rejects_symlink_without_changing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim.lua"
            victim.write_text("-- victim\n", encoding="utf-8")
            binding = root / "bindings.lua"
            binding.symlink_to(victim)

            result = self.run_helper("install", "--file", str(binding))

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(victim.read_text(encoding="utf-8"), "-- victim\n")

    def test_remove_rejects_symlink_without_changing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim.lua"
            original = f"{BEGIN}\nmanaged\n{END}\n"
            victim.write_text(original, encoding="utf-8")
            binding = root / "bindings.lua"
            binding.symlink_to(victim)

            result = self.run_helper("remove", "--file", str(binding))

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(victim.read_text(encoding="utf-8"), original)

    def test_install_rejects_symlink_in_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_parent = root / "real"
            real_parent.mkdir()
            victim = real_parent / "bindings.lua"
            victim.write_text("-- victim\n", encoding="utf-8")
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            result = self.run_helper(
                "install", "--file", str(linked_parent / "bindings.lua")
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(victim.read_text(encoding="utf-8"), "-- victim\n")

    def test_mutating_actions_reject_non_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binding = Path(directory) / "bindings.lua"
            os.mkfifo(binding)
            commands = (
                ("set", "SUPER + CTRL + K", "--file", str(binding)),
                ("install", "--file", str(binding)),
                ("remove", "--file", str(binding)),
            )

            for command in commands:
                with self.subTest(action=command[0]):
                    result = self.run_helper(*command)
                    self.assertNotEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertTrue(stat.S_ISFIFO(binding.lstat().st_mode))

    def test_install_set_and_remove_use_atomic_replacement_and_safe_backups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = root / "bindings.lua"
            original = "-- user binding\n"
            binding.write_text(original, encoding="utf-8")
            binding.chmod(0o640)

            previous_inode = binding.stat().st_ino
            install = self.run_helper("install", "--file", str(binding))
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            self.assertNotEqual(binding.stat().st_ino, previous_inode)
            self.assertEqual(stat.S_IMODE(binding.stat().st_mode), 0o640)
            self.assertIn('o.bind("SUPER + ALT + P"', binding.read_text(encoding="utf-8"))

            previous_inode = binding.stat().st_ino
            set_result = self.run_helper(
                "set", "SUPER + CTRL + K", "--file", str(binding)
            )
            self.assertEqual(
                set_result.returncode, 0, set_result.stdout + set_result.stderr
            )
            self.assertNotEqual(binding.stat().st_ino, previous_inode)
            self.assertEqual(stat.S_IMODE(binding.stat().st_mode), 0o640)
            self.assertIn('o.bind("SUPER + CTRL + K"', binding.read_text(encoding="utf-8"))

            previous_inode = binding.stat().st_ino
            remove = self.run_helper("remove", "--file", str(binding))
            self.assertEqual(remove.returncode, 0, remove.stdout + remove.stderr)
            self.assertNotEqual(binding.stat().st_ino, previous_inode)
            self.assertEqual(binding.read_text(encoding="utf-8"), original)
            self.assertEqual(stat.S_IMODE(binding.stat().st_mode), 0o640)

            backups = sorted(root.glob("bindings.lua.bak.omarchy-search-*"))
            self.assertEqual(len(backups), 3)
            for backup in backups:
                self.assertTrue(stat.S_ISREG(backup.lstat().st_mode))
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertFalse(list(root.glob(".bindings.lua.tmp.omarchy-search-*")))

    def test_failed_atomic_replace_leaves_live_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = root / "bindings.lua"
            original = "-- user binding\n"
            binding.write_text(original, encoding="utf-8")
            original_inode = binding.stat().st_ino

            with (
                mock.patch.object(bindings.os, "replace", side_effect=OSError("injected")),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(OSError, "injected"),
            ):
                bindings.cmd_set(binding, "SUPER + CTRL + K")

            self.assertEqual(binding.read_text(encoding="utf-8"), original)
            self.assertEqual(binding.stat().st_ino, original_inode)
            self.assertFalse(list(root.glob(".bindings.lua.tmp.omarchy-search-*")))

    def test_backup_file_and_replacement_are_synced_with_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binding = Path(directory) / "bindings.lua"
            binding.write_text("-- user binding\n", encoding="utf-8")
            synced_types: list[str] = []
            real_fsync = os.fsync

            def record_fsync(file_descriptor: int) -> None:
                mode = os.fstat(file_descriptor).st_mode
                synced_types.append("directory" if stat.S_ISDIR(mode) else "file")
                real_fsync(file_descriptor)

            with (
                mock.patch.object(bindings.os, "fsync", side_effect=record_fsync),
                redirect_stdout(io.StringIO()),
            ):
                bindings.cmd_set(binding, "SUPER + CTRL + K")

            self.assertEqual(
                synced_types,
                ["file", "directory", "file", "directory"],
            )


if __name__ == "__main__":
    unittest.main()
