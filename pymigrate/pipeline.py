"""Coordinate the migration lifecycle without executing target-project code.

Planning keeps original byte snapshots and proposed edits in memory. Application
verifies all outputs before writing, checks snapshots for concurrent changes,
and replaces files one at a time. Expected failures become report issues;
repository-wide rollback and target-test execution are outside this module.
"""

import ast
import io
import os
from pathlib import Path
import stat
import tempfile
import tokenize

from .models import Issue, Plan, Report, RULES, SourceFile
from .rules import analyze, rewrite

# Names are pruned at every directory depth, not treated as path patterns.
DEFAULT_EXCLUDES = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules", "build", "dist"}
)
# LookupError includes unknown encoding names; RecursionError covers sources
# whose nesting exceeds the parser/analysis recursion limits.
SOURCE_ERRORS = (SyntaxError, UnicodeError, LookupError, ValueError, RecursionError)


def _parse(source: str, filename: str) -> ast.Module:
    """Return an AST after checking both parsing and compilation.

    Parsing alone accepts some invalid contexts, such as a module-level return.
    Compilation rejects those without importing or executing the source.
    ``filename`` supplies a useful location in any resulting exception.
    """
    tree = ast.parse(source, filename=filename)
    compile(tree, filename, "exec")
    return tree


def create_plan(root: Path, excludes: set[str] | None = None) -> Plan:
    """Analyze a directory and return snapshots, candidate edits and issues.

    ``excludes`` adds directory names to the defaults. Symlink entries are not
    followed. Eligible files are counted before reading, so failed reads/parses
    still contribute to files_scanned.

    Expected per-file and traversal failures are collected so callers can see
    multiple problems at once. No files are modified, and any collected issue
    will prevent apply_plan from writing.
    """
    root = Path(root).absolute()
    plan = Plan(root)
    if root.is_symlink() or not root.is_dir():
        plan.issues.append(Issue(str(root), "Repository must be a non-symlink directory."))
        return plan
    excluded = DEFAULT_EXCLUDES | (excludes or set())

    def walk_error(error: OSError) -> None:
        """Keep directory-listing failures visible instead of silently skipping."""
        plan.issues.append(Issue(str(error.filename or root), str(error)))

    for directory, directories, filenames in os.walk(root, onerror=walk_error, followlinks=False):
        # os.walk uses this list in place to decide which subdirectories to visit.
        directories[:] = sorted(
            name
            for name in directories
            if name not in excluded and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.suffix != ".py" or path.is_symlink():
                continue
            plan.files_scanned += 1
            try:
                original = path.read_bytes()
                # Reading bytes first preserves coding cookies, BOMs and newlines.
                encoding, _ = tokenize.detect_encoding(io.BytesIO(original).readline)
                source = original.decode(encoding)
                tree = _parse(source, str(path))
                occurrences, edits = analyze(source, tree, str(path.relative_to(root)))
                plan.files.append(
                    SourceFile(path, original, source, encoding, occurrences, edits)
                )
            except (OSError, *SOURCE_ERRORS) as error:
                plan.issues.append(Issue(str(path.relative_to(root)), str(error)))
    return plan


def atomic_write(path: Path, content: bytes) -> None:
    """Write bytes through a sibling temporary file and replace the destination.

    The parent directory must exist. A sibling temporary file keeps os.replace
    on the same filesystem. Existing permission bits are copied, but this is
    not a promise to preserve every ACL, timestamp or other metadata.

    Filesystem errors propagate to the caller. Cleanup is attempted even when
    writing or replacement fails; atomicity is per file, not per repository.
    """
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            # Flush Python buffers before asking the OS to sync the file data.
            os.fsync(stream.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            # A copied read-only mode must not prevent temporary-file cleanup.
            temporary.chmod(stat.S_IREAD | stat.S_IWRITE)
            temporary.unlink()


def apply_plan(plan: Plan, *, allow_risky: bool = False) -> Report:
    """Stage selected edits, verify them, and replace files with verified bytes.

    Only low-risk rules are selected unless allow_risky is True. Planning,
    staging or initial snapshot errors prevent all writes. During writing, the
    first failure stops further replacements, but earlier changes remain and
    are reflected in the report's counters.

    PASS covers generated syntax, selected supported-candidate elimination and
    byte readback. It does not certify runtime behavior or run project tests.
    Snapshot checks reduce accidental overwrites but do not lock the files.
    """
    report = Report.from_plan(plan, "apply")
    selected = {rule.id for rule in RULES if allow_risky or rule.risk == "low"}
    report.changes_skipped = sum(
        not occurrence.supported or occurrence.rule not in selected
        for occurrence in plan.occurrences
    )
    if report.issues:
        report.verification = "FAIL"
        return report

    # Keep every verified output in memory before starting the write phase.
    # Each tuple contains the snapshot, serialized output and call-site count.
    staged: list[tuple[SourceFile, bytes, int]] = []
    for file in plan.files:
        edits = [edit for rule in selected for edit in file.edits[rule]]
        if not edits:
            continue
        try:
            updated = rewrite(file.source, edits)
            tree = _parse(updated, str(file.path))
            remaining, _ = analyze(updated, tree, str(file.path))
            # Syntax validity alone would not detect a no-op transformation.
            if any(item.supported and item.rule in selected for item in remaining):
                raise ValueError("Selected migration candidates remain after transformation.")
            encoded = updated.encode(file.encoding)
            # Validate the actual serialized bytes, including any coding cookie or BOM.
            compile(encoded, str(file.path), "exec")
            # Multiple source edits (including an import) still represent one call.
            count = sum(
                item.supported and item.rule in selected for item in file.occurrences
            )
            staged.append((file, encoded, count))
        except SOURCE_ERRORS as error:
            report.issues.append(Issue(str(file.path), f"Verification failed: {error}"))

    # Check all snapshots before the first write, including unchanged scanned files.
    for file in plan.files:
        try:
            if file.path.is_symlink() or file.path.read_bytes() != file.original:
                raise ValueError("File changed since scanning; create a fresh plan.")
        except (OSError, ValueError) as error:
            report.issues.append(Issue(str(file.path), str(error)))
    if report.issues:
        report.verification = "FAIL"
        return report

    for file, encoded, count in staged:
        try:
            # Recheck just before replacement; this is detection, not locking.
            if file.path.is_symlink() or file.path.read_bytes() != file.original:
                raise ValueError("File changed before replacement; stopping.")
            atomic_write(file.path, encoded)
            # Count successful replacements even if subsequent readback fails.
            report.files_modified += 1
            report.changes_applied += count
            if file.path.read_bytes() != encoded:
                raise ValueError("Written file does not match verified output.")
        except (OSError, ValueError) as error:
            report.issues.append(Issue(str(file.path), f"Apply failed: {error}"))
            break
    report.verification = "FAIL" if report.issues else "PASS"
    return report


def verify_repository(root: Path, excludes: set[str] | None = None) -> Report:
    """Re-scan current files and report whether parsing/compilation/analysis pass.

    No source is modified or executed. Remaining deprecated calls do not cause
    FAIL here; only recorded scan issues do. Use an apply report to check that
    the selected supported transformations were eliminated.
    """
    plan = create_plan(root, excludes)
    report = Report.from_plan(plan, "verify")
    report.verification = "FAIL" if report.issues else "PASS"
    return report
