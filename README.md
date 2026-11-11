# PyMigrate

A dependency-free, AST-guided Python migration framework:

**Scan -> Plan -> Apply -> Verify -> Report**

See [Architecture](ARCHITECTURE.md) for module responsibilities, data flow,
transformation design, verification guarantees and current limitations.

Requires Python 3.10 or later. Run with the Python version used by the target
repository: syntax verification uses the running interpreter, not every Python
version that the repository supports.

## Quick start

From this directory, no installation is needed:

```powershell
python -m pymigrate scan C:\work\my-repository
python -m pymigrate plan C:\work\my-repository --json
python -m pymigrate apply C:\work\my-repository
python -m pymigrate apply C:\work\my-repository --allow-risky --report migration.json
python -m pymigrate verify C:\work\my-repository
```

Alternatively, install with `python -m pip install -e .` and use `pymigrate`
instead of `python -m pymigrate`.

`scan` and `plan` are read-only and produce the same detailed occurrence list,
including file, line, column, rule, risk and unsupported-call reasons. `apply`
always builds a fresh in-memory plan; it does not load a saved JSON report as an
executable plan. `verify` checks syntax and compilability without changing files.
Every command accepts `--json`, `--report PATH` and repeatable
`--exclude DIRECTORY_NAME` options. Report parent directories must already exist;
Python source files and symbolic links cannot be report destinations.

## Exactly three rules

| Rule | Before | After | Risk |
|---|---|---|---|
| `logging-warn` | `logging.warn("msg")` | `logging.warning("msg")` | Low |
| `time-clock` | `time.clock()` | `time.perf_counter()` | Medium |
| `path-exists` | `os.path.exists(path)` | `_pymigrate_Path(path).exists()` | High |

The path rule inserts `from pathlib import Path as _pymigrate_Path`, choosing a
non-conflicting alias. The alias avoids collisions with existing `Path` symbols.

Only the logging rule is applied by default. **`--allow-risky` opts into both
other rules**:

- `perf_counter()` measures elapsed time. On Unix, `clock()` measured CPU time,
  so these are not interchangeable for every application.
- `Path` does not accept bytes paths or integer file descriptors.
  Exception behavior for invalid or inaccessible paths also depends on the
  Python version. Review the arguments before opting in.
- The path rule requires exactly one positional argument. Keyword, starred,
  missing and multiple arguments are reported but not changed.

Qualified aliases (`import logging as log`, `import os.path as paths`) and
direct imports are supported. A direct import such as
`from time import clock as timer` becomes
`from time import perf_counter as timer`, preserving its local binding and
removing the obsolete import. This also changes non-call uses of that binding
when the file contains a matching call.

## Safety and verification

- Uses the Python AST to identify calls and source spans, applying targeted edits
  instead of reformatting the entire file. Comments, surrounding formatting,
  source encoding, UTF-8 BOMs and ordinary LF/CRLF line endings are preserved.
- Matches imported standard-library APIs, not arbitrary objects named `logging`,
  `time` or `os`. Only unconditional module-level imports are supported.
- Conservatively skips an imported name throughout a file if any scope rebinds
  or shadows it. Wildcard imports disable matching for that file. Attribute
  assignments through the imported name also disable matching.
- Skips symbolic-link files/directories and common generated directories:
  `.git`, `.hg`, `.svn`, `.venv`, `venv`, `__pycache__`, `node_modules`, `build`,
  and `dist`. Additional exclusions match directory names at any depth.
- Syntax and compilation errors, unreadable sources and directory traversal
  errors are reported. Any planning or staging error prevents all writes.
- Parses and compiles every proposed output, checks serialized source bytes,
  and verifies that selected, supported candidates no longer remain.
- Detects changed source snapshots before writing. Each file is replaced
  atomically on its own filesystem, preserving permission bits, and its written
  bytes are checked against the verified output.

**Limits:** This is static analysis, not a proof of runtime equivalence. It does
not resolve local modules shadowing standard-library names, dynamic imports,
`exec`, runtime monkey-patching or reassignment via external code. Run it on a
trusted repository with known imports. It does not import the scanned code or
run the target project's tests. Run those tests and inspect the diff afterward.

Writes are atomic **per file**, not a repository-wide transaction. A disk or
permission failure during application stops further writes and reports the
actual number already modified; earlier successful writes are not rolled back.
Avoid concurrent edits during application: snapshot checks are not a locking
protocol. Use a clean version-control working tree or a backup before applying.

## Reports and exit codes

Text reports include:

```text
Files Scanned: 120
Files Affected: 18
Occurrences: 273
Files Modified: 18
Changes Applied: 273
Changes Skipped: 0
Verification: PASS
```

`--json` and `--report` include the same summary plus occurrences, rule notes,
and issues. Counts represent matched call sites, not import edits. Unsupported
and non-opted-in calls count as skipped on application.

`PASS` on application means syntax/compilation, selected-rule elimination and
written-byte verification passed; skipped candidates may still remain. `PASS`
on `verify` means syntax/compilation passed only. Read-only scan/plan reports
show `NOT RUN`. No claim of passing the target project's runtime tests is made.

Exit codes: `0` for success, `1` for scan/verification/I/O failure, and `2` for
invalid command-line arguments. A report-write failure returns `1`; if an
application already completed, its source edits remain in place.

## Example files

The `examples` directory contains reusable migration inputs:

| File | Scenario |
|---|---|
| `logging_usage.py` | One low-risk logging migration |
| `timing_usage.py` | One opt-in timing migration |
| `path_usage.py` | One opt-in string-path migration |
| `aliased_imports.py` | All three rules through module aliases |
| `direct_imports.py` | All three rules through direct import aliases |
| `skipped_cases.py` | Shadowed logging binding and two unsupported path calls |
| `already_modern.py` | Modern APIs and migration-like text left unchanged |

These are migration fixtures, not runnable applications. In particular,
`time.clock` no longer exists on modern Python, and the unsupported path calls
are intentionally included for scanning rather than execution.

Scan the originals, but apply to a disposable copy to keep the fixtures reusable:

```powershell
python -m pymigrate plan .\examples
Copy-Item -Recurse .\examples .\examples-sandbox
python -m pymigrate apply .\examples-sandbox --allow-risky --report examples-report.json
python -m pymigrate verify .\examples-sandbox
```

Use a new destination for each copy. A fresh copy has **7 files**, **11
occurrences**, and **6 affected files**. With `--allow-risky`, application modifies
**5 files**, applies **9 changes**, and skips **2 unsupported calls**. Without
that flag, it modifies **3 files**, applies **3 logging changes**, and skips
**8 calls**. The shadowed binding is not counted as a candidate.

## Development

```powershell
python -m unittest discover -s tests -v
```

Tests cover the three rules, aliases, direct imports, ambiguity, risk gating,
format/encoding preservation, idempotence, runtime behavior of generated code,
CLI reports, syntax failures, stale plans and partial-write errors. Symlink tests
are skipped when the operating system does not allow creating symlinks.

## Layout

- `pymigrate/models.py`: rules, occurrences, plans and reports.
- `pymigrate/rules.py`: import resolution and AST-guided transformations.
- `pymigrate/pipeline.py`: scanning, staging, verification and file replacement.
- `pymigrate/cli.py`: command parsing and reporting.
- `tests/`: standard-library `unittest` regression suite.
- `examples/`: reusable migration inputs, exercised on temporary copies in tests.
