from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from pymigrate.cli import main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "sample.py"
        self.source.write_text("import logging\nlogging.warn('hello')\n", encoding="utf-8")

    def invoke(self, *args: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main(list(args))
        return result, output.getvalue(), error.getvalue()

    def test_scan_and_plan_never_write(self) -> None:
        original = self.source.read_bytes()
        for command in ("scan", "plan"):
            code, output, _ = self.invoke(command, str(self.root), "--json")
            payload = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(payload["files_scanned"], 1)
            self.assertEqual(payload["files_affected"], 1)
            self.assertEqual(payload["occurrences"][0]["rule"], "logging-warn")
            self.assertEqual(payload["occurrences"][0]["risk"], "low")
            self.assertEqual(self.source.read_bytes(), original)

    def test_apply_text_report_and_json_file(self) -> None:
        report_path = self.root / "migration.json"
        code, output, _ = self.invoke(
            "apply", str(self.root), "--report", str(report_path)
        )
        self.assertEqual(code, 0)
        self.assertIn("Files Modified: 1", output)
        self.assertIn("Changes Applied: 1", output)
        self.assertIn("Verification: PASS", output)
        self.assertEqual(json.loads(report_path.read_text())["changes_applied"], 1)

    def test_failure_returns_nonzero_with_error(self) -> None:
        self.source.write_text("def broken(")
        code, output, _ = self.invoke("verify", str(self.root), "--json")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["verification"], "FAIL")
        self.assertTrue(payload["issues"])

    def test_report_write_error_is_explicit(self) -> None:
        code, _, error = self.invoke(
            "plan", str(self.root), "--report", str(self.root / "missing" / "report.json")
        )
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", error)

    def test_report_cannot_overwrite_python(self) -> None:
        original = self.source.read_bytes()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["apply", str(self.root), "--report", str(self.source)])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.source.read_bytes(), original)

    def test_module_entry_point(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "pymigrate", "verify", str(self.root), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verification"], "PASS")

    def test_full_cli_pipeline_for_all_rules(self) -> None:
        self.source.write_text(
            "import logging, time, os\nlogging.warn('hello')\n"
            "elapsed = time.clock()\nfound = os.path.exists('.')\n"
        )
        for command, options in (
            ("plan", []),
            ("apply", ["--allow-risky"]),
            ("verify", []),
            ("apply", ["--allow-risky"]),
        ):
            result = subprocess.run(
                [sys.executable, "-m", "pymigrate", command, str(self.root), "--json", *options],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            if command == "plan":
                self.assertEqual(len(payload["occurrences"]), 3)
                self.assertEqual(payload["files_modified"], 0)
            else:
                self.assertEqual(payload["verification"], "PASS")
                if payload["occurrences"]:
                    self.assertEqual(payload["changes_applied"], 3)
                    self.assertEqual(payload["files_modified"], 1)
                else:
                    self.assertEqual(payload["changes_applied"], 0)
        self.assertIn("logging.warning", self.source.read_text())
        self.assertIn("time.perf_counter()", self.source.read_text())
        self.assertIn("_pymigrate_Path('.').exists()", self.source.read_text())

    def test_examples_pipeline_on_temporary_copy(self) -> None:
        examples = Path(__file__).resolve().parents[1] / "examples"
        originals = {path.name: path.read_bytes() for path in examples.glob("*.py")}
        target = self.root / "examples"
        shutil.copytree(examples, target, ignore=shutil.ignore_patterns("__pycache__"))

        code, output, error = self.invoke("plan", str(target), "--json")
        self.assertEqual(code, 0, error)
        plan = json.loads(output)
        self.assertEqual(plan["files_scanned"], 7)
        self.assertEqual(plan["files_affected"], 6)
        self.assertEqual(len(plan["occurrences"]), 11)
        self.assertEqual(sum(item["supported"] for item in plan["occurrences"]), 9)

        code, output, error = self.invoke("apply", str(target), "--json")
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["changes_applied"], 3)
        self.assertEqual(report["changes_skipped"], 8)
        self.assertEqual(report["files_modified"], 3)
        self.assertEqual(report["verification"], "PASS")

        code, output, error = self.invoke("apply", str(target), "--allow-risky", "--json")
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["changes_applied"], 6)
        self.assertEqual(report["changes_skipped"], 2)
        self.assertEqual(report["files_modified"], 4)
        self.assertEqual(report["verification"], "PASS")

        code, output, error = self.invoke("verify", str(target), "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["verification"], "PASS")
        code, output, error = self.invoke("apply", str(target), "--allow-risky", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["changes_applied"], 0)
        for name in ("skipped_cases.py", "already_modern.py"):
            self.assertEqual((target / name).read_bytes(), originals[name])

        fresh_target = self.root / "all-rules"
        shutil.copytree(examples, fresh_target, ignore=shutil.ignore_patterns("__pycache__"))
        code, output, error = self.invoke(
            "apply", str(fresh_target), "--allow-risky", "--json"
        )
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["changes_applied"], 9)
        self.assertEqual(report["changes_skipped"], 2)
        self.assertEqual(report["files_modified"], 5)
        self.assertEqual(report["verification"], "PASS")
        self.assertEqual(
            {path.name: path.read_bytes() for path in examples.glob("*.py")}, originals
        )


if __name__ == "__main__":
    unittest.main()
