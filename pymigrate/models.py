"""Data passed between analysis, application and reporting.

Frozen dataclasses describe individual facts or edits. Mutable containers collect
those facts while the pipeline runs. These models do not read files, rewrite
source, or format output.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Risk = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Rule:
    """Describe a built-in migration, not its implementation.

    ``source`` is the fully qualified API used for matching. ``replacement`` is
    descriptive metadata; rules.py supplies the actual edits. ``risk`` controls
    default selection, and ``caveat`` explains behavioral differences to users.
    """

    id: str
    source: str
    replacement: str
    risk: Risk
    caveat: str


# Adding metadata here alone does not implement a rule; matching and edit
# generation must also be supported by rules.py.
RULES = (
    Rule(
        "logging-warn",
        "logging.warn",
        "logging.warning",
        "low",
        "Uses the non-deprecated spelling of the logging API.",
    ),
    Rule(
        "time-clock",
        "time.clock",
        "time.perf_counter",
        "medium",
        "perf_counter measures elapsed time; clock measured CPU time on Unix.",
    ),
    Rule(
        "path-exists",
        "os.path.exists",
        "Path(path).exists()",
        "high",
        "Path does not accept bytes paths or file descriptors; exception behavior "
        "also depends on the Python version.",
    ),
)


@dataclass(frozen=True)
class Occurrence:
    """Record one matched call, including calls that cannot be rewritten.

    ``file`` is normally relative to the scanned root. Lines and columns are
    1-based; columns reflect AST UTF-8 byte positions, not display characters.
    ``supported`` describes call shape, not whether the user selected the rule.
    Unsupported calls carry an explanation in ``reason``.
    """

    file: str
    line: int
    column: int
    rule: str
    risk: Risk
    supported: bool = True
    reason: str = ""


@dataclass(frozen=True)
class Issue:
    """Associate an expected failure with a source path or repository location."""

    file: str
    message: str


@dataclass(frozen=True)
class Edit:
    """Replace source[start:end] with replacement, using string indexes.

    The range is half-open: start is included and end is excluded. Equal start
    and end values insert text without deleting anything. Offsets refer to the
    original decoded source, so edits must be applied from right to left.
    """

    start: int
    end: int
    replacement: str


@dataclass
class SourceFile:
    """Keep the original file snapshot and the proposed per-rule edits.

    ``original`` bytes detect changes since scanning. ``source`` and ``encoding``
    allow edits to be made as text and serialized in the original encoding.
    ``edits`` groups replacements by rule ID so risk selection can happen later.
    """

    path: Path
    original: bytes
    source: str
    encoding: str
    occurrences: list[Occurrence]
    edits: dict[str, list[Edit]]


@dataclass
class Plan:
    """Hold an in-memory scan result, including failures and unchanged files.

    ``files_scanned`` counts eligible files attempted, even if a read or parse
    failed. ``files`` contains only successfully analyzed snapshots. A plan with
    any issues cannot be applied; reports are summaries, not replayable plans.
    """

    root: Path
    files_scanned: int = 0
    files: list[SourceFile] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def occurrences(self) -> list[Occurrence]:
        """Flatten per-file matches into a new list in scan order."""
        return [item for file in self.files for item in file.occurrences]


@dataclass
class Report:
    """Summarize an operation for text output or JSON serialization.

    ``files_affected`` includes any file with matches, even unsupported ones.
    Application counters count replaced files and migrated call sites, not the
    number of edit objects. ``changes_skipped`` counts unsupported or unselected
    calls during application.

    ``verification`` is NOT RUN for scan/plan, otherwise PASS or FAIL. PASS does
    not imply runtime equivalence or that the target project's tests were run.
    """

    operation: str
    root: str
    files_scanned: int
    files_affected: int
    occurrences: list[Occurrence]
    files_modified: int = 0
    changes_applied: int = 0
    changes_skipped: int = 0
    verification: str = "NOT RUN"
    issues: list[Issue] = field(default_factory=list)

    @classmethod
    def from_plan(cls, plan: Plan, operation: str) -> "Report":
        """Build a summary with application counters left at zero.

        Copy the issue list so later report errors do not modify the plan's
        issue list. The contained immutable occurrences and issues can be shared.
        """
        return cls(
            operation=operation,
            root=str(plan.root),
            files_scanned=plan.files_scanned,
            files_affected=sum(bool(file.occurrences) for file in plan.files),
            occurrences=plan.occurrences,
            issues=list(plan.issues),
        )
