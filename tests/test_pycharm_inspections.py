r"""Headless PyCharm inspection gate.

Runs the bundled PyCharm CLI inspector once over a single root scope
(``-d <project root>``; recursion covers main.py, sonos_pc_streamer/
and tests/).  The scope includes ``.venv`` so the run takes ~2 minutes
— accepted, because findings outside first-party code are filtered in
the parser via _FIRST_PARTY_PREFIXES, not by narrowing the inspection
scope.  The same .idea/inspectionProfiles/Project_Default.xml profile
used in the GUI is passed explicitly so the gate matches the project
baseline.

The IDE's inspect.bat is resolved without machine-specific literals:
the PYCHARM_INSPECT_BAT environment variable overrides, otherwise the
newest inspect.bat under the standard JetBrains install roots of the
real local drives wins.  The private isolate (idea.properties plus the
wiped-every-run config/system caches) lives under
%LOCALAPPDATA%\SonosPCstreamer\inspector and is fully self-healing:
idea.properties is rewritten on every run, and the interpreter fixture
is re-copied from the newest %APPDATA%\JetBrains\*\options\jdk.table.xml
before invoking inspect.bat — without that fixture the wiped config has
no interpreter and the inspector reports bogus "Package ... is not
installed" / unresolved-stub findings.

Skipped when inspect.bat cannot be located, when the project profile
cannot be found, or when no jdk.table.xml fixture exists.
"""

import glob as _glob
import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import psutil

_ROOT = Path(__file__).resolve().parents[1]
_PROFILE = _ROOT / ".idea" / "inspectionProfiles" / "Project_Default.xml"

_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA",
                                    str(Path.home() / "AppData" / "Local")))
_ISOLATE_DIR = _LOCALAPPDATA / "SonosPCstreamer" / "inspector"
_IDEA_PROPERTIES = _ISOLATE_DIR / "idea.properties"

_IDEA_PROPERTIES_TEMPLATE = (
    "idea.config.path={0}/config\n"
    "idea.system.path={0}/system\n"
    "idea.plugins.path={0}/plugins\n"
    "idea.log.path={0}/log\n"
)

# Findings outside these prefixes (.venv, ffmpeg/, .idea, README.md,
# requirements.txt, ruff.toml, ...) are third-party or GUI-managed
# surface outside this gate's contract; they are still scanned because
# the inspector runs on the single project root.
_FIRST_PARTY_PREFIXES = (
    "file://$PROJECT_DIR$/main.py",
    "file://$PROJECT_DIR$/sonos_pc_streamer/",
    "file://$PROJECT_DIR$/tests/",
)


def _find_inspect_bat():
    """Resolve the IDE's CLI inspector without machine-specific literals.

    Order: the PYCHARM_INSPECT_BAT environment variable (explicit escape
    hatch when several PyCharm installs exist), then the newest
    inspect.bat under the standard JetBrains install roots of the real
    local drives.  Returns None when nothing is found (the gate skips).
    """
    override = os.environ.get("PYCHARM_INSPECT_BAT")
    if override and Path(override).is_file():
        return Path(override)

    drive_letters = []
    for part in psutil.disk_partitions():
        device = part.device or ""
        if len(device) >= 2 and device[1] == ":" and part.fstype:
            if "remote" in part.opts or "cdrom" in part.opts:
                continue
            drive_letters.append(device[0])
    if not drive_letters:
        drive_letters = ["C"]

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)",
                                       r"C:\Program Files (x86)")
    patterns = [
        rf"{program_files}\JetBrains\PyCharm*\bin\inspect.bat",
        rf"{program_files_x86}\JetBrains\PyCharm*\bin\inspect.bat",
        rf"{_LOCALAPPDATA}\Programs\JetBrains\PyCharm*\bin\inspect.bat",
    ]
    patterns += [
        rf"{letter}:\Program Files\JetBrains\PyCharm*\bin\inspect.bat"
        for letter in drive_letters
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(_glob.glob(pattern))
    candidates = [Path(c) for c in candidates if Path(c).is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


_INSPECT_BAT = _find_inspect_bat()


def _require_inspect_bat() -> Path:
    """Definite-path accessor: raises SkipTest when the inspector is absent."""
    if _INSPECT_BAT is None:
        raise unittest.SkipTest("PyCharm CLI inspector (inspect.bat) not found; "
                                "set PYCHARM_INSPECT_BAT to override the search")
    return _INSPECT_BAT


def _write_idea_properties():
    """(Re)write the isolate's idea.properties; content is deterministic."""
    _ISOLATE_DIR.mkdir(parents=True, exist_ok=True)
    _IDEA_PROPERTIES.write_text(
        _IDEA_PROPERTIES_TEMPLATE.format(_ISOLATE_DIR.as_posix()),
        encoding="utf-8")


def _parse_problems(output_dir: Path):
    """Return (file_path, line_number, inspection_class, description) tuples."""
    problems = []
    for xml_file in sorted(output_dir.glob("*.xml")):
        if xml_file.name == ".descriptions.xml":
            continue
        try:
            tree = ET.parse(xml_file)
        except ET.ParseError:
            continue
        for problem_el in tree.iter("problem"):
            fc = problem_el.find("problem_class")
            inspection_class = (fc.text or "") if fc is not None else xml_file.stem
            desc_el = problem_el.find("description")
            description = (desc_el.text or "") if desc_el is not None else ""
            file_el = problem_el.find("file")
            file_path = (file_el.text or "") if file_el is not None else ""
            line_el = problem_el.find("line")
            line_number = (line_el.text or "?") if line_el is not None else "?"
            if not file_path.startswith(_FIRST_PARTY_PREFIXES):
                continue
            problems.append((file_path, line_number, inspection_class,
                             description))
    return problems


@unittest.skipUnless(_INSPECT_BAT is not None and _INSPECT_BAT.exists(),
                     "PyCharm CLI inspector (inspect.bat) not found; set "
                     "PYCHARM_INSPECT_BAT to override the search")
class PyCharmInspectionGateTest(unittest.TestCase):
    def test_zero_findings_in_designated_scope(self):
        if not _PROFILE.exists():
            self.skipTest(f"Project profile not found: {_PROFILE}")
        inspect_bat = _require_inspect_bat()

        tmp_out = Path(tempfile.mkdtemp(prefix="pyc_gate_"))
        try:
            # The gate is fully self-healing: idea.properties is rewritten
            # on every run and the config/system caches are wiped so a cold
            # analysis is forced (warm caches repeat stale findings).
            for subdir in ("config", "system"):
                shutil.rmtree(_ISOLATE_DIR / subdir, ignore_errors=True)
            _write_idea_properties()
            # The wiped private config has no interpreter configured, so
            # re-create the SDK fixture from the newest GUI-side
            # jdk.table.xml before invoking inspect.bat (otherwise the
            # inspector reports bogus "Package ... is not installed" and
            # unresolved-stub findings).
            fixtures = sorted(
                (Path.home() / "AppData" / "Roaming" / "JetBrains")
                .glob("*/options/jdk.table.xml"),
                key=lambda p: p.stat().st_mtime,
            )
            if not fixtures:
                self.skipTest("jdk.table.xml fixture not found")
            options_dir = _ISOLATE_DIR / "config" / "options"
            options_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(fixtures[-1], options_dir / "jdk.table.xml")
            env = os.environ.copy()
            env["PYCHARM_PROPERTIES"] = str(_IDEA_PROPERTIES)
            cmd = [
                "cmd", "/C",
                str(inspect_bat),
                str(_ROOT),
                str(_PROFILE),
                str(tmp_out),
                "-d", str(_ROOT),
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                timeout=420, env=env,
            )
            if result.returncode != 0:
                self.fail(
                    f"inspect.bat exited with code {result.returncode}\n"
                    f"--- stdout (last 2 000 chars) ---\n"
                    f"{result.stdout[-2000:]}\n"
                    f"--- stderr (last 2 000 chars) ---\n"
                    f"{result.stderr[-2000:]}"
                )
            problems = _parse_problems(tmp_out)
            if problems:
                lines = ["PyCharm inspector found issues:"]
                for file_path, line_number, inspection_class, desc in problems:
                    lines.append(
                        f"  {file_path}:{line_number} [{inspection_class}] {desc}")
                self.fail("\n".join(lines))
        finally:
            shutil.rmtree(tmp_out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
