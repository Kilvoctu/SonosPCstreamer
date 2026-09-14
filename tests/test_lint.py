"""Ruff lint gate for the whole repository.

Runs ``ruff check .`` from the repo root so the ruff.toml in the root and
the .gitignore-exclusion rules (never scan .venv) apply. The run uses the
same interpreter that is executing the tests (``python -m ruff``, pinned to
``ruff==0.16.7`` in requirements.txt) with a fallback to a bare ``ruff`` on
PATH. On any finding the full ruff output is printed so the exact
file:line:code is visible in the test log.

A second check pins the intentional BLE001/B013 ignores documented in
ruff.toml, so a future cleanup of the defensive exception pattern is a
deliberate decision, not a silent config edit.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
RUFF_TOML = os.path.join(ROOT, "ruff.toml")


def _run_ruff():
    """Invoke ruff check on the repo root; return (returncode, output)."""
    candidates = (
        [sys.executable, "-m", "ruff", "check", "."],
        ["ruff", "check", "."],
    )
    errors = []
    for cmd in candidates:
        try:
            proc = subprocess.run(
                cmd, cwd=ROOT, capture_output=True, text=True, check=False,
                timeout=120, encoding="utf-8", errors="replace",
            )
            return proc.returncode, proc.stdout + proc.stderr
        except FileNotFoundError as exc:
            errors.append(" ".join(cmd) + f" ({exc})")
    raise AssertionError("ruff not found after trying: " + " ; ".join(errors))


class RuffLintTest(unittest.TestCase):
    def test_ruff_check_passes_on_whole_repo(self):
        returncode, output = _run_ruff()
        if returncode != 0:
            self.fail(f"ruff check found issues (exit {returncode}):\n{output}")

    def test_ruff_toml_pins_intentional_exception_ignores(self):
        self.assertTrue(os.path.isfile(RUFF_TOML),
                        f"missing {os.path.relpath(RUFF_TOML, ROOT)}")
        with open(RUFF_TOML, encoding="utf-8") as f:
            config = f.read()
        self.assertIn('"BLE001"', config,
                      "ruff.toml must keep BLE001 in lint.ignore")
        self.assertIn('"B013"', config,
                      "ruff.toml must keep B013 in lint.ignore")

    def test_no_nested_except_alias_shadowing_in_production(self):
        """No except-alias in a nested function shadows an enclosing frame's alias.

        PyCharm's PyShadowingNamesInspection flags exactly this shape (a
        closure or nested def whose ``except ... as NAME`` reuses an alias
        bound by an enclosing function's handler). Same-function repeated
        aliases are deliberately NOT flagged here, matching PyCharm.
        """
        import ast as _ast
        import pathlib as _pl

        class _Visitor(_ast.NodeVisitor):
            def __init__(self):
                self.stack = []   # one alias-set per active function/class frame
                self.findings = []

            def _enter(self, node):
                self.stack.append(set())
                self.generic_visit(node)
                self.stack.pop()

            visit_FunctionDef = _enter
            visit_AsyncFunctionDef = _enter
            visit_ClassDef = _enter

            def visit_ExceptHandler(self, node):
                name = node.name
                if name and self.stack:
                    if any(name in frame for frame in self.stack[:-1]):
                        self.findings.append((node.lineno, name))
                    self.stack[-1].add(name)
                self.generic_visit(node)

        roots = [_pl.Path(ROOT, "main.py")] + list(
            _pl.Path(ROOT, "sonos_pc_streamer").glob("*.py"))
        findings = []
        for root in roots:
            visitor = _Visitor()
            visitor.visit(_ast.parse(root.read_text(encoding="utf-8")))
            for lineno, alias_name in visitor.findings:
                findings.append(f"{root}:{lineno} {alias_name}")
        self.assertEqual(findings, [],
                         "Except-aliases in nested functions shadowing outer "
                         "frames:\n" + "\n".join(findings))


if __name__ == "__main__":
    unittest.main()