"""Translate command-line requests into pipeline operations and reports.

This module handles presentation and process status; scanning and source writes
belong to pipeline.py. Both the installed console script and ``python -m
pymigrate`` call ``main``.
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .models import Report, RULES
from .pipeline import apply_plan, atomic_write, create_plan, verify_repository


def _parser() -> argparse.ArgumentParser:
    """Build subcommands with shared output options and an apply-only risk gate.

    Roots are directories. Repeated --exclude values name directories to prune,
    not individual file patterns. argparse reports invalid usage with exit code 2.
    """
    parser = argparse.ArgumentParser(
        prog="pymigrate",
        description="Scan -> Plan -> Apply -> Verify -> Report Python API migrations.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "plan", "apply", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("root", type=Path, help="Repository directory")
        subparser.add_argument("--exclude", action="append", default=[], metavar="DIRECTORY")
        subparser.add_argument("--json", action="store_true", help="Print JSON instead of text")
        subparser.add_argument("--report", type=Path, help="Write a JSON report to a non-.py file")
        if command == "apply":
            subparser.add_argument(
                "--allow-risky",
                action="store_true",
                help="Also apply time.clock and os.path.exists migrations",
            )
    return parser


def _text_report(report: Report) -> str:
    """Render counts, source locations, relevant rule caveats and errors.

    The per-occurrence SKIPPED label explains unsupported call shapes. Risk-gated
    skips are reflected in the apply summary's count, not in that label.
    """
    lines = [
        f"Operation: {report.operation}",
        f"Files Scanned: {report.files_scanned}",
        f"Files Affected: {report.files_affected}",
        f"Occurrences: {len(report.occurrences)}",
        f"Files Modified: {report.files_modified}",
        f"Changes Applied: {report.changes_applied}",
        f"Changes Skipped: {report.changes_skipped}",
        f"Verification: {report.verification}",
    ]
    for occurrence in report.occurrences:
        detail = f" [SKIPPED: {occurrence.reason}]" if not occurrence.supported else ""
        lines.append(
            f"  {occurrence.file}:{occurrence.line}:{occurrence.column} "
            f"{occurrence.rule} ({occurrence.risk}){detail}"
        )
    if report.occurrences:
        lines.append("Rule notes:")
        used = {item.rule for item in report.occurrences}
        lines.extend(f"  {rule.id}: {rule.caveat}" for rule in RULES if rule.id in used)
    lines.extend(f"ERROR {issue.file}: {issue.message}" for issue in report.issues)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run one CLI operation and return its process status.

    Passing argv supports programmatic calls and tests; None uses sys.argv.
    Return 0 on success or 1 for recorded issues/output I/O errors. Invalid
    arguments raise SystemExit(2) through argparse.

    Every apply builds a fresh plan. JSON output is a report, not a saved plan
    that can later be replayed. A report-write failure can occur after source
    changes have completed; it does not roll those changes back.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    # Validate report destinations before an apply can modify source files.
    if args.report is not None:
        if args.report.suffix.lower() == ".py":
            parser.error("--report cannot overwrite a Python source file")
        if args.report.is_symlink():
            parser.error("--report cannot target a symbolic link")
    try:
        if args.command == "verify":
            report = verify_repository(args.root, set(args.exclude))
        else:
            plan = create_plan(args.root, set(args.exclude))
            if args.command == "apply":
                report = apply_plan(plan, allow_risky=args.allow_risky)
            else:
                # scan and plan intentionally share the same read-only behavior.
                report = Report.from_plan(plan, args.command)
        # Serialize only the report, never source snapshots or executable edits.
        payload = asdict(report)
        payload["rules"] = [asdict(rule) for rule in RULES]
        serialized = json.dumps(payload, indent=2)
        if args.report is not None:
            atomic_write(args.report, (serialized + "\n").encode("utf-8"))
        print(serialized if args.json else _text_report(report))
    except OSError as error:
        # Pipeline source errors are already issues; this also handles output I/O.
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 1 if report.issues else 0
