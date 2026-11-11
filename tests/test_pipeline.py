import ast
import codecs
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pymigrate.models import Edit
from pymigrate.pipeline import apply_plan, atomic_write, create_plan, verify_repository
from pymigrate.rules import rewrite


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, source: str, name: str = "sample.py") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.encode("utf-8"))
        return path

    def migrate(self, source: str, *, allow_risky: bool = True) -> str:
        path = self.write(source)
        report = apply_plan(create_plan(self.root), allow_risky=allow_risky)
        self.assertEqual(report.issues, [])
        self.assertEqual(report.verification, "PASS")
        return path.read_bytes().decode("utf-8")

    def test_three_rules_and_counts(self) -> None:
        path = self.write(
            'import logging\nimport time\nimport os\n'
            'logging.warn("hello")\na = time.clock()\nb = os.path.exists("sample.py")\n'
        )
        plan = create_plan(self.root)
        self.assertEqual(plan.files_scanned, 1)
        self.assertEqual([item.risk for item in plan.occurrences], ["low", "medium", "high"])
        report = apply_plan(plan, allow_risky=True)
        self.assertEqual(report.verification, "PASS")
        self.assertEqual(report.files_modified, 1)
        self.assertEqual(report.changes_applied, 3)
        source = path.read_text()
        self.assertIn('logging.warning("hello")', source)
        self.assertIn("time.perf_counter()", source)
        self.assertIn('_pymigrate_Path("sample.py").exists()', source)
        self.assertEqual(create_plan(self.root).occurrences, [])

    def test_risky_rules_require_opt_in(self) -> None:
        source = 'import logging, time, os\nlogging.warn("x")\ntime.clock()\nos.path.exists("x")\n'
        path = self.write(source)
        report = apply_plan(create_plan(self.root))
        self.assertEqual(report.changes_applied, 1)
        self.assertEqual(report.changes_skipped, 2)
        self.assertIn("time.clock()", path.read_text())
        self.assertNotIn("from pathlib", path.read_text())

    def test_aliases_and_direct_imports(self) -> None:
        source = (
            "import logging as log\nimport time as timer\nimport os.path as paths\n"
            "from logging import warn as old_warn\nfrom time import clock\n"
            "from os.path import exists as present\n"
            'log.warn("x")\ntimer.clock()\npaths.exists("x")\n'
            'old_warn("x")\nold_warn("y")\nclock()\npresent("x")\n'
        )
        result = self.migrate(source)
        self.assertIn("log.warning", result)
        self.assertIn("timer.perf_counter", result)
        self.assertEqual(result.count("from logging import warning as old_warn"), 1)
        self.assertIn("from time import perf_counter as clock", result)
        self.assertIn("old_warn('", result.replace('"', "'"))
        self.assertEqual(create_plan(self.root).occurrences, [])

    def test_unrelated_objects_and_text_are_unchanged(self) -> None:
        source = (
            '# logging.warn("comment")\ntext = "time.clock()"\n'
            'logger.warn("not the module")\nthing.exists("x")\n'
        )
        self.assertEqual(self.migrate(source), source)

    def test_ambiguous_names_are_not_rewritten(self) -> None:
        cases = [
            "import logging\nlogging = object()\nlogging.warn('x')\n",
            "import logging\ndef f(logging):\n    logging.warn('x')\n",
            "import logging\ndef logging(): pass\nlogging.warn('x')\n",
            "import logging\nlogging.warn = custom\nlogging.warn('x')\n",
            "import logging\nfrom custom import *\nlogging.warn('x')\n",
            "from . import logging\nlogging.warn('x')\n",
            "def f():\n    import logging\n    logging.warn('x')\n",
            "if True:\n    import logging\nlogging.warn('x')\n",
            "import logging\ntry: pass\nexcept Exception as logging: pass\nlogging.warn('x')\n",
            "import logging\nmatch x:\n    case {'a': logging}: pass\nlogging.warn('x')\n",
            "import logging\nmatch x:\n    case {**logging}: pass\nlogging.warn('x')\n",
            "import logging\n[logging.warn('x') for logging in values]\n",
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(self.migrate(source), source)

    def test_unsupported_path_signatures_are_reported(self) -> None:
        source = (
            "import os\nos.path.exists(path='x')\nos.path.exists(*args)\n"
            "os.path.exists()\nos.path.exists('a', 'b')\nos.path.exists(**kwargs)\n"
        )
        path = self.write(source)
        plan = create_plan(self.root)
        self.assertEqual(len(plan.occurrences), 5)
        self.assertTrue(all(not item.supported and item.reason for item in plan.occurrences))
        report = apply_plan(plan, allow_risky=True)
        self.assertEqual(report.changes_skipped, 5)
        self.assertEqual(report.files_modified, 0)
        self.assertEqual(path.read_text(), source)

    def test_nested_calls_comments_and_formatting(self) -> None:
        source = (
            "import os, logging\n"
            "# untouched\n"
            "result = os.path.exists(  # outer\n"
            '    str(os.path.exists("x"))  # inner\n'
            ")\n"
            "logging.\\\n"
            '    warn(  "x" )  # keep\n'
        )
        result = self.migrate(source)
        self.assertIn("# outer", result)
        self.assertIn("# inner", result)
        self.assertIn('str(_pymigrate_Path("x").exists())', result)
        self.assertIn('warning(  "x" )  # keep', result)
        self.assertIn("# untouched", result)

    def test_unicode_before_call_uses_correct_offsets(self) -> None:
        source = 'import logging\ntext = "caf\u00e9"; logging.warn("hello")\n'
        self.assertEqual(self.migrate(source), source.replace("logging.warn", "logging.warning"))

    def test_non_newline_separators_do_not_break_ast_offsets(self) -> None:
        for separator in ("\u2028", "\u2029", "\x0b", "\x0c"):
            with self.subTest(separator=repr(separator)):
                source = f'import logging\ntext = "{separator}"; logging.warn("x")\n'
                self.assertEqual(
                    self.migrate(source), source.replace("logging.warn", "logging.warning")
                )

    def test_lone_cr_newlines(self) -> None:
        result = self.migrate("import os\ros.path.exists('x')\r")
        self.assertIn("from pathlib import Path as _pymigrate_Path\r", result)
        self.assertIn("_pymigrate_Path('x').exists()", result)
        self.assertNotIn("\n", result)

    def test_encoding_cookie_and_crlf_are_preserved(self) -> None:
        original = b'# coding: latin-1\r\nimport os\r\nname = "caf\xe9"\r\nos.path.exists(name)\r\n'
        path = self.root / "sample.py"
        path.write_bytes(original)
        report = apply_plan(create_plan(self.root), allow_risky=True)
        self.assertEqual(report.verification, "PASS")
        result = path.read_bytes()
        self.assertIn(b"caf\xe9", result)
        self.assertTrue(result.startswith(b"# coding: latin-1\r\n"))
        self.assertNotIn(b"\n", result.replace(b"\r\n", b""))

    def test_utf8_bom_is_preserved(self) -> None:
        path = self.root / "sample.py"
        path.write_bytes(codecs.BOM_UTF8 + b"import os\nos.path.exists('x')\n")
        report = apply_plan(create_plan(self.root), allow_risky=True)
        self.assertEqual(report.verification, "PASS")
        self.assertTrue(path.read_bytes().startswith(codecs.BOM_UTF8))

    def test_docstring_future_header_and_decorator_are_preserved(self) -> None:
        source = (
            '#!/usr/bin/env python\n# coding: utf-8\n"""Module docs."""\n'
            "from __future__ import annotations\n"
            "import os\n"
            "@decorator\n"
            "def check():\n    return os.path.exists('x')\n"
        )
        result = self.migrate(source)
        tree = ast.parse(result)
        self.assertEqual(ast.get_docstring(tree), "Module docs.")
        self.assertTrue(result.startswith("#!/usr/bin/env python\n# coding: utf-8\n"))
        self.assertLess(result.index("from __future__"), result.index("from pathlib"))
        self.assertIn("@decorator\ndef check()", result)

    def test_import_inserted_before_initial_decorator(self) -> None:
        result = self.migrate(
            "@decorator\n"
            "def check():\n    return os.path.exists('x')\n"
            "import os\n"
        )
        self.assertTrue(result.startswith("from pathlib"))
        self.assertIn("@decorator\ndef check()", result)

    def test_semicolon_import_after_future(self) -> None:
        result = self.migrate(
            '"""Docs"""; from __future__ import annotations; import os; '
            "os.path.exists('x')"
        )
        self.assertEqual(ast.get_docstring(ast.parse(result)), "Docs")
        self.assertLess(result.index("from __future__"), result.index("from pathlib"))

    def test_generated_path_name_does_not_collide(self) -> None:
        result = self.migrate(
            "from logging import warn\nimport os\n"
            "_pymigrate_Path = object()\n"
            "warn('x')\nos.path.exists('x')\n"
        )
        self.assertIn("import Path as _pymigrate_Path_", result)
        self.assertIn("from logging import warning as warn", result)

    def test_idempotence(self) -> None:
        self.migrate("import os, logging, time\nlogging.warn('x')\ntime.clock()\nos.path.exists('x')\n")
        report = apply_plan(create_plan(self.root), allow_risky=True)
        self.assertEqual(report.files_modified, 0)
        self.assertEqual(report.changes_applied, 0)
        self.assertEqual(report.verification, "PASS")

    def test_excluded_directories_and_non_python_files(self) -> None:
        self.write("invalid!", ".venv\\bad.py")
        self.write("invalid!", "generated\\bad.py")
        self.write("invalid!", "note.txt")
        self.write("x = 1", "src\\good.py")
        plan = create_plan(self.root, {"generated"})
        self.assertEqual(plan.issues, [])
        self.assertEqual(plan.files_scanned, 1)

    def test_invalid_repository(self) -> None:
        plan = create_plan(self.root / "missing")
        self.assertEqual(len(plan.issues), 1)
        self.assertEqual(apply_plan(plan).verification, "FAIL")

    def test_parse_or_compile_error_prevents_all_writes(self) -> None:
        for bad in ("def broken(", "return 1", "x = '\x00'"):
            with self.subTest(bad=bad):
                path = self.write("import logging\nlogging.warn('x')\n")
                original = path.read_bytes()
                self.write(bad, "bad.py")
                report = apply_plan(create_plan(self.root))
                self.assertEqual(report.verification, "FAIL")
                self.assertEqual(report.files_modified, 0)
                self.assertEqual(path.read_bytes(), original)

    def test_invalid_encoding_is_reported(self) -> None:
        (self.root / "bad.py").write_bytes(b"# coding: nonsense\nx = 1\n")
        self.assertTrue(create_plan(self.root).issues)

    def test_scan_read_error_is_reported(self) -> None:
        self.write("x = 1")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            plan = create_plan(self.root)
        self.assertIn("denied", plan.issues[0].message)

    def test_walk_error_is_reported(self) -> None:
        def fail_walk(root, *, onerror, followlinks):
            self.assertEqual(root, self.root)
            self.assertFalse(followlinks)
            onerror(PermissionError("cannot list"))
            return iter(())

        with patch("pymigrate.pipeline.os.walk", side_effect=fail_walk):
            plan = create_plan(self.root)
        self.assertIn("cannot list", plan.issues[0].message)

    def test_stale_plan_prevents_all_writes(self) -> None:
        first = self.write("import logging\nlogging.warn('x')\n", "a.py")
        second = self.write("x = 1\n", "b.py")
        original = first.read_bytes()
        plan = create_plan(self.root)
        second.write_text("x = 2\n")
        report = apply_plan(plan)
        self.assertEqual(report.verification, "FAIL")
        self.assertEqual(first.read_bytes(), original)

    def test_invalid_generated_source_prevents_all_writes(self) -> None:
        path = self.write("import logging\nlogging.warn('x')\n")
        plan = create_plan(self.root)
        with patch("pymigrate.pipeline.rewrite", return_value="def broken("):
            report = apply_plan(plan)
        self.assertEqual(report.verification, "FAIL")
        self.assertIn("logging.warn", path.read_text())

    def test_noop_transform_is_rejected(self) -> None:
        source = "import logging\nlogging.warn('x')\n"
        self.write(source)
        with patch("pymigrate.pipeline.rewrite", return_value=source):
            report = apply_plan(create_plan(self.root))
        self.assertEqual(report.verification, "FAIL")
        self.assertIn("candidates remain", report.issues[0].message)

    def test_write_failure_is_explicit_and_original_survives(self) -> None:
        path = self.write("import logging\nlogging.warn('x')\n")
        original = path.read_bytes()
        with patch("pymigrate.pipeline.os.replace", side_effect=PermissionError("denied")):
            report = apply_plan(create_plan(self.root))
        self.assertEqual(report.verification, "FAIL")
        self.assertEqual(report.files_modified, 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_partial_apply_reports_actual_counts_and_stops(self) -> None:
        self.write("import logging\nlogging.warn('a')\n", "a.py")
        self.write("import logging\nlogging.warn('b')\n", "b.py")
        calls = 0

        def fail_second(path: Path, content: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            atomic_write(path, content)

        with patch("pymigrate.pipeline.atomic_write", side_effect=fail_second):
            report = apply_plan(create_plan(self.root))
        self.assertEqual(report.verification, "FAIL")
        self.assertEqual(report.files_modified, 1)
        self.assertEqual(report.changes_applied, 1)
        self.assertIn("disk full", report.issues[0].message)

    def test_verify_does_not_execute_repository_code(self) -> None:
        self.write("raise RuntimeError('must not execute')\n")
        report = verify_repository(self.root)
        self.assertEqual(report.verification, "PASS")

    def test_migrated_direct_imports_execute_on_modern_python(self) -> None:
        result = self.migrate(
            "from time import clock\nfrom logging import warn as old_warn\n"
            "def run():\n    old_warn('message')\n    return clock()\n"
        )
        namespace: dict[str, object] = {}
        with patch("logging.warning") as warning, patch("time.perf_counter", return_value=42):
            exec(compile(result + "\nanswer = run()\n", "<migrated>", "exec"), namespace)
        self.assertEqual(namespace["answer"], 42)
        warning.assert_called_once_with("message")

    def test_path_expression_is_evaluated_once(self) -> None:
        result = self.migrate(
            "import os\ncalls = []\n"
            "def get_path():\n    calls.append(1)\n    return '.'\n"
            "answer = os.path.exists(get_path())\n"
        )
        namespace: dict[str, object] = {}
        exec(compile(result, "<migrated>", "exec"), namespace)
        self.assertIs(namespace["answer"], True)
        self.assertEqual(namespace["calls"], [1])

    def test_symlink_file_is_not_followed(self) -> None:
        target = self.write("import logging\nlogging.warn('x')\n", "target.txt")
        link = self.root / "linked.py"
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest(f"Symlinks not available: {error}")
        self.assertEqual(create_plan(self.root).files_scanned, 0)

    def test_symlink_entries_are_skipped_without_filesystem_privileges(self) -> None:
        path = self.write("import logging\nlogging.warn('x')\n")
        real_is_symlink = Path.is_symlink

        def is_symlink(candidate: Path) -> bool:
            return candidate == path or real_is_symlink(candidate)

        with patch.object(Path, "is_symlink", is_symlink):
            self.assertEqual(create_plan(self.root).files_scanned, 0)

    def test_overlap_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            rewrite("abcd", [Edit(0, 3, "a"), Edit(2, 4, "b")])


if __name__ == "__main__":
    unittest.main()
