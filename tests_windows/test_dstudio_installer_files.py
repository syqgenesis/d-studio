"""Static checks of the packaging files (portable: no Windows, Inno or PowerShell needed).

Catches the mistakes that would only show up on the Windows build machine:
pins without hashes, encodings Windows tools misread, a changed AppId, an
uninstaller that could touch recordings, unbalanced PowerShell, LF-only .bat.
"""
from __future__ import annotations

import json
import os
import re
import unittest
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(ROOT, "installer")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def read(path, mode="r"):
    with open(os.path.join(ROOT, path), "rb") as fh:
        data = fh.read()
    return data if mode == "rb" else data.decode("utf-8-sig")


def strip_ps_strings_and_comments(text: str) -> str:
    """Remove PowerShell comments, here-strings and quoted strings (rough lexer)."""
    text = re.sub(r"@'.*?\n'@", "''", text, flags=re.S)
    text = re.sub(r'@".*?\n"@', '""', text, flags=re.S)
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and j + 1 < n and text[j + 1] == "'":
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            out.append("''")
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "`" else 1
            out.append('""')
            i = j + 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "`":
            out.append(text[i:i + 2])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class Pins(unittest.TestCase):
    def test_pins_json(self):
        pins = json.loads(read("installer/pins.json"))
        for key in ("python", "ffmpeg"):
            pin = pins[key]
            self.assertTrue(pin["url"].startswith("https://"), key)
            self.assertRegex(pin["sha256"], HEX64, key)
            self.assertEqual(unquote(pin["url"].rsplit("/", 1)[1]), pin["file"], key)
        self.assertIn("x86_64-pc-windows-msvc-install_only", pins["python"]["file"])
        self.assertTrue(pins["python"]["version"].startswith("3.12."))

    def test_requirements_build_fully_hashed(self):
        # a Windows checkout (core.autocrlf) gives CRLF line ends
        text = read("requirements-build.txt").replace("\r\n", "\n").replace("\\\n", " ")
        reqs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        names = set()
        for ln in reqs:
            m = re.match(r"^([A-Za-z0-9_.-]+)==([0-9][^ ]*)\s+(--hash=sha256:[0-9a-f]{64}\s*)+$", ln)
            self.assertIsNotNone(m, ln)
            names.add(m.group(1).lower())
        self.assertEqual(names, {"numpy", "comtypes", "bettercam", "pillow"})

    def test_requirements_txt_declares_its_encoding(self):
        # pip decodes requirement files with the locale code page (cp936 / cp1252) unless
        # the first two lines carry a PEP 263 coding comment; ours contain Chinese.
        raw = read("requirements.txt", "rb")
        first_two = raw.split(b"\n")[:2]
        self.assertTrue(any(ln.startswith(b"#") and re.search(rb"coding[:=]\s*utf-8", ln) for ln in first_two))
        names = {re.split(r"[<>=!;\s]", ln.strip())[0].lower() for ln in raw.decode().splitlines()
                 if ln.strip() and not ln.startswith("#")}
        self.assertTrue({"numpy", "bettercam", "comtypes", "pillow"} <= names)


class InnoScript(unittest.TestCase):
    def setUp(self):
        self.raw = read("installer/dstudio.iss", "rb")
        self.text = self.raw.decode("utf-8-sig")

    def test_utf8_bom(self):
        self.assertTrue(self.raw.startswith(b"\xef\xbb\xbf"), "Inno needs a BOM to read the Chinese text as UTF-8")

    def test_per_user_fixed_appid(self):
        self.assertIn("PrivilegesRequired=lowest", self.text)
        self.assertIn(r"DefaultDirName={localappdata}\Programs\DStudio", self.text)
        self.assertRegex(self.text, r"AppId=\{\{6E0B5F2C-8D3A-4B7E-9C21-5A4F7D1E3B90\}")
        self.assertNotIn("PrivilegesRequiredOverridesAllowed", self.text)

    def test_languages(self):
        self.assertIn('Name: "en"; MessagesFile: "compiler:Default.isl"', self.text)
        self.assertIn('Name: "zh"; MessagesFile: "{#ZhIsl}"', self.text)
        for key in ("AnnotatorName", "SelfTestName", "RecordingsName", "RecordingsKept"):
            self.assertIn(f"en.{key}=", self.text)
            self.assertIn(f"zh.{key}=", self.text)

    def test_shortcuts(self):
        icons = self.section("[Icons]")
        self.assertIn('{app}\\python\\pythonw.exe', self.text)
        self.assertIn('--viewer', icons)
        self.assertIn('--selftest', icons)
        self.assertIn('#define PyFlags "-E -s"', self.text)
        self.assertIn("Tasks: desktopicon", icons)

    def test_uninstall_never_touches_recordings(self):
        for section in ("[UninstallDelete]", "[InstallDelete]"):
            body = self.section(section)
            self.assertIn("Type:", body, section)
            for line in body.splitlines():
                if line.startswith("Type:"):
                    self.assertNotIn("{userdocs}", line)
                    self.assertNotIn("\\data", line)
                    self.assertNotIn("{userappdata}", line)
                    if "filesandordirs" in line:
                        self.assertNotRegex(line, r'Name: "\{app\}"\s*$', "would delete the whole folder")
        self.assertIn('Name: "{userdocs}\\DStudio"; Flags: uninsneveruninstall', self.text)

    def section(self, name: str) -> str:
        """Body of an Inno section; the header must start a line (comments may mention section names)."""
        m = re.search(r"(?m)^" + re.escape(name) + r"\r?$", self.text)
        self.assertIsNotNone(m, name)
        return self.text[m.end():].split("\n[", 1)[0]

    def test_no_dir_page_and_deletes_only_in_our_folder(self):
        # finding 23: a typed-in existing folder (D:\\Tools) must never lose its python\\ or docs\\
        setup = self.section("[Setup]")
        self.assertRegex(setup, r"(?m)^DisableDirPage=yes\r?$")
        self.assertIn("UsePreviousAppDir=yes", setup)
        for section in ("[InstallDelete]", "[UninstallDelete]"):
            entries = [ln.strip() for ln in self.section(section).splitlines() if "filesandordirs" in ln]
            self.assertTrue(entries, section)
            for line in entries:
                self.assertRegex(line, r"; Check: IsOurInstall$", f"{section}: {line}")
        code = self.section("[Code]")
        self.assertIn("function IsOurInstall: Boolean;", code)
        self.assertIn("'\\INSTALLED'", code)
        self.assertIn("function InitializeUninstall: Boolean;", code)
        for name in ("python", "ffmpeg", "recorder", "packager", "viewer", "tools", "schemas", "docs"):
            self.assertIn(f"DirExists(App + '\\{name}')", code, name)   # every folder the entries remove

    def test_recordings_shortcut_and_uninstall_message_follow_settings(self):
        # finding 34 / K8: the Start-menu entry and the "kept in" message use settings data_dir
        icons = self.section("[Icons]")
        rec = next(ln for ln in icons.splitlines() if "{cm:RecordingsName}" in ln)
        self.assertIn('Filename: "{#PyW}"', rec)
        self.assertIn('""{app}\\dstudio.pyw"" --open-data', rec)
        self.assertIn("{#PyFlags}", rec)
        self.assertNotIn('Filename: "{userdocs}', icons)
        for key in ("RecordingsComment",):
            self.assertIn(f"en.{key}=", self.text)
            self.assertIn(f"zh.{key}=", self.text)
        code = self.section("[Code]")
        self.assertIn("{userappdata}\\DStudio\\settings.json", code)
        self.assertIn("'\"data_dir\"'", code)
        self.assertIn("{userdocs}\\DStudio", code)
        self.assertIn("MultiByteToWideChar@kernel32.dll stdcall", code)   # settings.json is UTF-8
        self.assertIn("CustomMessage('RecordingsKept')", code)
        # the launcher really has the option the shortcut passes
        launcher = read("dstudio.pyw")
        self.assertIn('"--open-data"', launcher)

    def test_pascal_code_is_balanced(self):
        code = self.section("[Code]")
        code = re.sub(r"//[^\n]*", "", code)
        code = re.sub(r"'(?:[^']|'')*'", "''", code)
        code = re.sub(r"\{[^}]*\}", "", code)
        words = re.findall(r"[A-Za-z_]+", code.lower())
        self.assertEqual(words.count("begin"), words.count("end"), "begin/end")
        self.assertEqual(code.count("("), code.count(")"), "()")
        self.assertEqual(code.count("["), code.count("]"), "[]")
        # every routine the entries / Inno call is defined exactly once
        for routine in ("function IsOurInstall", "function InitializeUninstall", "procedure CurUninstallStepChanged",
                        "function SettingsDataDir", "function Utf8ToStr"):
            self.assertEqual(self.section("[Code]").count(routine), 1, routine)
        self.assertNotRegex(self.section("[Code]"), r"(?m)^\s*#", "ISPP would read a line starting with #")

    def test_chinese_messages_file(self):
        raw = read("installer/lang/ChineseSimplified.isl", "rb")
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("[LangOptions]", text)
        self.assertIn("LanguageID=$0804", text)
        self.assertIn("CreateDesktopIcon=", text)


class PowerShell(unittest.TestCase):
    SCRIPTS = ("installer/build_windows.ps1", "installer/common.ps1", "installer/get_ffmpeg.ps1")

    def test_ascii_only(self):
        # Windows PowerShell 5.1 reads BOM-less scripts as ANSI: keep them ASCII
        for path in self.SCRIPTS:
            raw = read(path, "rb")
            self.assertTrue(all(b < 128 for b in raw), path)

    def test_balanced_brackets(self):
        for path in self.SCRIPTS:
            code = strip_ps_strings_and_comments(read(path))
            for o, c in ("{}", "()", "[]"):
                self.assertEqual(code.count(o), code.count(c), f"{path}: {o}{c} unbalanced")

    def test_strict_error_handling(self):
        for path in ("installer/build_windows.ps1", "installer/get_ffmpeg.ps1"):
            self.assertIn("$ErrorActionPreference = 'Stop'", read(path))
        common = read("installer/common.ps1")
        self.assertIn("System32\\tar.exe", common, "must not pick up Git's GNU tar")

    def test_build_reads_pins_and_marker(self):
        text = read("installer/build_windows.ps1")
        self.assertIn("--require-hashes", text)
        self.assertIn("'INSTALLED'", text)
        self.assertIn("DStudio-Portable-$Version.zip", text)
        self.assertIn("DStudio-Setup-$Version.exe", text)


class BatchFiles(unittest.TestCase):
    NAMES = ("INSTALL.bat", "SELFTEST.bat", "START-ALL.bat", "START-OVERLAY.bat", "START-VIEWER.bat",
             "START-GDIGRAB.bat", "overlay.bat")

    def test_crlf_no_bom_and_codepage(self):
        for name in self.NAMES:
            raw = read(name, "rb")
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{name}: a BOM breaks the first command")
            self.assertNotRegex(raw, rb"[^\r]\n", f"{name}: LF-only line endings confuse cmd.exe labels")
            lines = raw.split(b"\r\n")
            first_non_ascii = next((i for i, ln in enumerate(lines) if any(b > 127 for b in ln)), None)
            if first_non_ascii is not None:
                chcp = next((i for i, ln in enumerate(lines) if ln.strip().lower().startswith(b"chcp 65001")), None)
                self.assertIsNotNone(chcp, f"{name}: non-ASCII text needs 'chcp 65001' first")
                self.assertLess(chcp, first_non_ascii, name)

    def test_launch_through_dstudio_pyw(self):
        for name in ("START-ALL.bat", "START-OVERLAY.bat", "START-VIEWER.bat", "START-GDIGRAB.bat", "SELFTEST.bat"):
            text = read(name)
            self.assertIn("dstudio.pyw", text, name)
            self.assertIn('if exist "%HERE%python\\python.exe"', text, name)


class Workflow(unittest.TestCase):
    def test_workflow_structure(self):
        path = os.path.join(ROOT, ".github", "workflows", "windows.yml")
        text = read(".github/workflows/windows.yml")
        self.assertNotIn("\t", text)
        for ref in re.findall(r"uses:\s*(\S+)", text):
            self.assertRegex(ref, r"^actions/[a-z-]+@v\d+$", "pin actions to a major version")
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        jobs = data["jobs"]
        self.assertEqual(set(jobs), {"unit-ubuntu", "windows"})
        self.assertEqual(jobs["windows"]["runs-on"], "windows-latest")
        self.assertEqual(data["permissions"], {"contents": "read"})
        names = " ".join(s.get("name", "") for s in jobs["windows"]["steps"])
        for word in ("screenshots", "E2E", "installer", "self-test"):
            self.assertIn(word, names)
        on = data.get("on", data.get(True))
        self.assertIn("push", on)
        self.assertIn("workflow_dispatch", on)


if __name__ == "__main__":
    unittest.main()
