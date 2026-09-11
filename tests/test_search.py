from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse, quote

from scripts import search


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "search.py"


class UrlBuilderTests(unittest.TestCase):
    def test_text_url_url_encodes_query(self) -> None:
        self.assertEqual(
            search.text_url("google", "house keys"),
            "https://www.google.com/search?q=house%20keys",
        )
        self.assertEqual(
            search.text_url("youtube", "a&b"),
            "https://www.youtube.com/results?search_query=a%26b",
        )

    def test_lens_url_encodes_image_url(self) -> None:
        url = search.lens_url("https://x.test/a.png?size=1")
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "lens.google.com")
        self.assertEqual(parsed.path, "/uploadbyurl")
        self.assertEqual(
            parse_qs(parsed.query)["url"],
            ["https://x.test/a.png?size=1"],
        )

    def test_bing_visual_url_embeds_imgurl(self) -> None:
        url = search.bing_visual_url("https://x.test/a b.png")
        self.assertIn("view=detailv2&iss=sbi&form=SBIHMP", url)
        self.assertTrue(url.endswith("&q=imgurl:" + quote("https://x.test/a b.png", safe="")))
        parsed = parse_qs(urlparse(url).query)
        self.assertEqual(parsed["q"], ["imgurl:https://x.test/a b.png"])

    def test_claude_prompt_uses_markdown_image(self) -> None:
        prompt = search.prompt_for_image("claude", "what is this?", "https://x.test/i.png")
        self.assertEqual(prompt, "![image](https://x.test/i.png)\nwhat is this?")

    def test_claude_prompt_without_text(self) -> None:
        self.assertEqual(
            search.prompt_for_image("claude", "", "https://x.test/i.png"),
            "![image](https://x.test/i.png)",
        )

    def test_chatgpt_prompt_lists_image_url_after_text(self) -> None:
        prompt = search.prompt_for_image("chatgpt", "summarize", "https://x.test/i.png")
        self.assertEqual(prompt, "summarize\nhttps://x.test/i.png")

    def test_search_url_dispatches_by_engine_kind(self) -> None:
        image = "https://x.test/i.png"
        self.assertIn("lens.google.com", search.search_url("google", "text", image))
        self.assertIn("bing.com/images/search", search.search_url("bing", "text", image))
        chatgpt = search.search_url("chatgpt", "look", image)
        self.assertTrue(chatgpt.startswith("https://chatgpt.com/?q="))
        self.assertEqual(
            parse_qs(urlparse(chatgpt).query)["q"],
            ["look\nhttps://x.test/i.png"],
        )
        self.assertEqual(
            search.search_url("github", "hello", image),
            "https://github.com/search?q=hello",
        )

    def test_search_url_without_text_for_non_image_engine_opens_image(self) -> None:
        self.assertEqual(
            search.search_url("github", "", "https://x.test/i.png"),
            "https://x.test/i.png",
        )


class ImageKindTests(unittest.TestCase):
    def test_all_text_engines_have_prefixes(self) -> None:
        for engine_id in search.TEXT_PREFIXES:
            self.assertTrue(search.TEXT_PREFIXES[engine_id])

    def test_image_kinds_are_known(self) -> None:
        for engine_id, kind in search.IMAGE_KINDS.items():
            self.assertIn(engine_id, search.TEXT_PREFIXES)
            self.assertIn(kind, ("lens", "bing", "prompt"))


class AttachFallbackTests(unittest.TestCase):
    def test_history_image_path_finds_latest_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "latest.png"
            image.write_bytes(b"png")
            later_text = {"type": "text", "text": "words"}
            earlier_image = {"type": "image", "path": str(image)}
            with mock.patch.object(
                search, "OMARCHY_HISTORY", history_file(directory, [later_text, earlier_image])
            ):
                self.assertEqual(search.history_image_path(), image)

    def test_history_image_path_ignores_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "gone.png"
            with mock.patch.object(
                search, "OMARCHY_HISTORY", history_file(directory, [{"type": "image", "path": str(missing)}])
            ):
                self.assertIsNone(search.history_image_path())


class CliTests(unittest.TestCase):
    def run_helper(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_unknown_engine_exits_error(self) -> None:
        result = self.run_helper("search", "--engine", "nope", "--query", "x")
        self.assertEqual(result.returncode, 2)

    def test_unknown_host_exits_error(self) -> None:
        result = self.run_helper("search", "--engine", "google", "--query", "x", "--host", "nope")
        self.assertEqual(result.returncode, 2)


class HostSelectionTests(unittest.TestCase):
    def test_auto_chain_prompt_starts_with_catbox(self) -> None:
        self.assertEqual(search.host_chain("prompt", "auto"), ["catbox", "0x0", "litterbox"])

    def test_auto_chain_lens_starts_with_litterbox(self) -> None:
        self.assertEqual(search.host_chain("lens", "auto"), ["litterbox", "catbox", "0x0"])

    def test_auto_chain_bing_starts_with_litterbox(self) -> None:
        self.assertEqual(search.host_chain("bing", "auto"), ["litterbox", "catbox", "0x0"])

    def test_explicit_host_leads_chain(self) -> None:
        self.assertEqual(search.host_chain("prompt", "litterbox"), ["litterbox", "catbox", "0x0"])
        self.assertEqual(search.host_chain("lens", "0x0"), ["0x0", "catbox", "litterbox"])

    def test_unknown_host_falls_back_to_auto_chain(self) -> None:
        self.assertEqual(search.host_chain("prompt", "nope"), ["catbox", "0x0", "litterbox"])


class UploadTests(unittest.TestCase):
    def completed(self, args: list[str], code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr=stderr)

    def test_curl_args_for_catbox(self) -> None:
        args = search._curl_args(Path("/tmp/a.png"), "catbox", 30)
        self.assertIn("fileToUpload=@/tmp/a.png", args)
        self.assertEqual(args[-1], search.CATBOX_API)

    def test_curl_args_for_litterbox_include_expiry(self) -> None:
        args = search._curl_args(Path("/tmp/a.png"), "litterbox", 30)
        self.assertIn("fileToUpload=@/tmp/a.png", args)
        self.assertIn("time=1h", args)
        self.assertEqual(args[-1], search.LITTERBOX_API)

    def test_curl_args_for_0x0_use_file_field(self) -> None:
        args = search._curl_args(Path("/tmp/a.png"), "0x0", 30)
        self.assertIn("file=@/tmp/a.png", args)
        self.assertEqual(args[-1], search.ZER0X_API)

    def test_upload_returns_first_success(self) -> None:
        used: list[str] = []

        def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            used.append(args[-1])
            return self.completed(args, 0, stdout="https://files.catbox.moe/x.png\n")

        with mock.patch.object(search.subprocess, "run", side_effect=fake_run):
            url = search.upload_image(Path("/tmp/a.png"), kind="prompt", host="auto")
        self.assertEqual(url, "https://files.catbox.moe/x.png")
        self.assertEqual(used, [search.CATBOX_API])

    def test_upload_falls_back_to_next_host(self) -> None:
        used: list[str] = []

        def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            used.append(args[-1])
            if args[-1] == search.CATBOX_API:
                return self.completed(args, 1, stderr="curl: (22) the requested URL returned error: 403\n")
            return self.completed(args, 0, stdout="https://0x0.st/a.png\n")

        with mock.patch.object(search.subprocess, "run", side_effect=fake_run):
            url = search.upload_image(Path("/tmp/a.png"), kind="prompt", host="auto")
        self.assertEqual(url, "https://0x0.st/a.png")
        self.assertEqual(used, [search.CATBOX_API, search.CATBOX_API, search.ZER0X_API])

    def test_upload_raises_when_all_hosts_fail(self) -> None:
        def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return self.completed(args, 1, stderr="curl: (7) failed to connect\n")

        with (
            mock.patch.object(search.subprocess, "run", side_effect=fake_run),
            self.assertRaises(search.UploadError),
        ):
            search.upload_image(Path("/tmp/a.png"), kind="prompt", host="0x0")


class CmdSearchTests(unittest.TestCase):
    def test_cmd_search_passes_host_and_kind_to_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "i.png"
            image.write_bytes(b"png")
            url = "https://files.catbox.moe/x.png"
            with (
                mock.patch.object(search, "upload_image", return_value=url) as upload,
                mock.patch.object(search, "launch") as launch,
            ):
                code = search.cmd_search("chatgpt", "look", str(image), host="catbox")
            self.assertEqual(code, 0)
            upload.assert_called_once_with(image, kind="prompt", host="catbox")
            launch.assert_called_once_with(search.search_url("chatgpt", "look", url))

    def test_cmd_search_auto_uses_default_host_for_lens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "i.png"
            image.write_bytes(b"png")
            url = "https://litter.catbox.moe/abc.png"
            with (
                mock.patch.object(search, "upload_image", return_value=url) as upload,
                mock.patch.object(search, "launch"),
            ):
                code = search.cmd_search("google", "", str(image))
            self.assertEqual(code, 0)
            upload.assert_called_once_with(image, kind="lens", host="auto")


class CmdAttachTests(unittest.TestCase):
    def test_cmd_attach_without_image_exits_one(self) -> None:
        with (
            mock.patch.object(search, "clipboard_image_data", return_value=None),
            mock.patch.object(search, "history_image_path", return_value=None),
        ):
            self.assertEqual(search.cmd_attach(), 1)

    def test_cmd_attach_writes_clipboard_image_and_prints_path(self) -> None:
        written: list[Path] = []
        fake_write = lambda destination, data: written.append(destination)

        output = io.StringIO()
        with (
            mock.patch.object(search, "clipboard_image_data", return_value=b"pngdata"),
            mock.patch.object(Path, "write_bytes", autospec=True, side_effect=fake_write),
            redirect_stdout(output),
        ):
            self.assertEqual(search.cmd_attach(), 0)

        line = output.getvalue().strip()
        self.assertTrue(line.endswith(".png"))
        self.assertTrue(Path(line).is_absolute())
        self.assertEqual(len(written), 1)

    def test_cmd_attach_falls_back_to_clipboard_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "old.png"
            source.write_bytes(b"pngdata")
            copied: list[Path] = []
            fake_copy = lambda src, dest: copied.append(dest)

            output = io.StringIO()
            with (
                mock.patch.object(search, "clipboard_image_data", return_value=None),
                mock.patch.object(search, "history_image_path", return_value=source),
                mock.patch.object(search.shutil, "copyfile", autospec=True, side_effect=fake_copy),
                redirect_stdout(output),
            ):
                self.assertEqual(search.cmd_attach(), 0)

            line = output.getvalue().strip()
            self.assertTrue(line.endswith(".png"))
            self.assertEqual(len(copied), 1)


def history_file(directory: str, entries: list[dict]) -> Path:
    path = Path(directory) / "clipboard-history.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()