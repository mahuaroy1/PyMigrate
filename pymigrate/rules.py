"""Find migration calls and describe precise edits without touching the disk.

The AST identifies imports, calls and source spans. Instead of regenerating an
entire AST, this module edits the original text to preserve surrounding comments
and formatting. Analysis proposes edits for all rules; pipeline.py decides which
risk levels to apply and verifies the resulting source.
"""

import ast
from collections import Counter
import io

from .models import Edit, Occurrence, RULES


def _dotted_name(node: ast.expr) -> str | None:
    """Return a simple attribute chain such as 'os.path.exists', or None.

    Only Name/Attribute chains qualify. Expressions such as get_logger().warn
    are deliberately not resolved because their runtime objects are unknown.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _bindings(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """Return accepted import bindings and all collected names in the file.

    The first result maps local names to qualified imports, for example
    {'log': 'logging'} or {'exists': 'os.path.exists'}. The second supports
    choosing a generated alias that does not collide with collected names.

    This is deliberately file-wide, not a scope-aware symbol table: a parameter
    or assignment anywhere can invalidate an imported name everywhere. Only a
    single counted binding from an unconditional top-level import is accepted.
    Wildcard imports disable all matching. Dynamic rebinding and local modules
    that shadow the standard library are not resolved.
    """
    imports: dict[str, str] = {}
    counts: Counter[str] = Counter()
    names: set[str] = set()
    wildcard = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                counts[node.id] += 1
        elif isinstance(node, ast.arg):
            # Parameters bind names without appearing as ast.Name(Store) nodes.
            names.add(node.arg)
            counts[node.arg] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            counts[node.name] += 1
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
            counts[node.name] += 1
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
            counts[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            # The **rest part of a mapping pattern also captures a local name.
            names.add(node.rest)
            counts[node.rest] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    wildcard = True
                    continue
                name = alias.asname or alias.name.split(".")[0]
                names.add(name)
                counts[name] += 1
                if isinstance(node, ast.Import):
                    # "import os.path" binds os; "import os.path as p" binds p.
                    imports[name] = alias.name if alias.asname else name
                elif node.level == 0:
                    imports[name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            # Writes like logging.warn = custom make the imported API uncertain.
            dotted = _dotted_name(node)
            if dotted:
                counts[dotted.split(".")[0]] += 1

    # File-wide ambiguity intentionally causes false negatives rather than unsafe rewrites.
    top_level_imports = {
        alias.asname or alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    return (
        {
            name: target
            for name, target in imports.items()
            if counts[name] == 1 and name in top_level_imports and not wildcard
        },
        names,
    )


class SourceOffsets:
    """Translate AST UTF-8 byte columns into indexes in decoded source text.

    AST line numbers are 1-based, while its columns count UTF-8 bytes from zero.
    Python string slices count characters, so directly using AST columns would
    target the wrong text after non-ASCII characters.
    """

    def __init__(self, source: str):
        """Keep original line endings and cumulative character offsets."""
        # Unlike str.splitlines, this does not treat Unicode separators inside
        # strings as additional Python source lines.
        self.lines = io.StringIO(source, newline="").readlines()
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))

    def at(self, line: int, byte_column: int) -> int:
        """Convert a valid AST (line, byte column) pair to a string index."""
        prefix = self.lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8")
        return self.starts[line - 1] + len(prefix)

    def span(self, node: ast.expr | ast.stmt) -> tuple[int, int]:
        """Return the node's half-open character range, rejecting missing ends."""
        if node.end_lineno is None or node.end_col_offset is None:
            raise ValueError("AST node has no source end position")
        return (
            self.at(node.lineno, node.col_offset),
            self.at(node.end_lineno, node.end_col_offset),
        )


def analyze(
    source: str, tree: ast.Module, filename: str
) -> tuple[list[Occurrence], dict[str, list[Edit]]]:
    """Find known API calls and propose edits grouped by rule ID.

    ``tree`` must describe ``source``; ``filename`` is a report label only.
    Unsupported path-call shapes are included as occurrences but get no edits.
    Ambiguous bindings are excluded before matching, so they are not reported
    as occurrences.

    This function does not mutate the AST, change files or apply risk selection.
    It returns locations sorted by source order and edits in original-source
    coordinates for rewrite to apply later.
    """
    bindings, names = _bindings(tree)
    offsets = SourceOffsets(source)
    occurrences: list[Occurrence] = []
    edits: dict[str, list[Edit]] = {rule.id: [] for rule in RULES}
    path_alias = "_pymigrate_Path"
    # One collision-avoiding alias is shared by all migrated path calls.
    while path_alias in names:
        path_alias += "_"
    needs_path_import = False
    # Map direct-call local names to rule IDs for a single import edit per name.
    direct_imports: dict[str, str] = {}
    by_source = {rule.source: rule for rule in RULES}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        if dotted is None:
            continue
        head, separator, tail = dotted.partition(".")
        if head not in bindings:
            continue
        # Expand aliases before matching: log.warn -> logging.warn.
        qualified = bindings[head] + (separator + tail if separator else "")
        rule = by_source.get(qualified)
        if rule is None:
            continue
        # Path construction only supports the unambiguous single-argument form.
        # Argument runtime types are not inferred, hence this rule's risk gate.
        supported = rule.id != "path-exists" or (
            len(node.args) == 1
            and not isinstance(node.args[0], ast.Starred)
            and not node.keywords
        )
        occurrences.append(
            Occurrence(
                filename,
                node.lineno,
                node.col_offset + 1,
                rule.id,
                rule.risk,
                supported,
                "" if supported else "Path migration requires exactly one positional argument.",
            )
        )
        if not supported:
            continue
        start, end = offsets.span(node.func)
        if rule.id == "path-exists":
            # Keep argument text intact, including comments and nested calls:
            # os.path.exists(argument) -> alias(argument).exists().
            edits[rule.id].append(Edit(start, end, path_alias))
            _, call_end = offsets.span(node)
            edits[rule.id].append(Edit(call_end, call_end, ".exists()"))
            needs_path_import = True
        else:
            replacement = "warning" if rule.id == "logging-warn" else "perf_counter"
            if isinstance(node.func, ast.Attribute):
                # Replace only the attribute, preserving aliasing and intervening comments.
                edits[rule.id].append(Edit(end - len(node.func.attr), end, replacement))
            else:
                # Changing only clock() would leave an obsolete direct import.
                # Change its imported API instead, keeping the local call name.
                direct_imports[head] = rule.id

    # Preserve the local binding, but remove imports of APIs absent on modern Python.
    by_id = {rule.id: rule for rule in RULES}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local_name = alias.asname or alias.name
                rule_id = direct_imports.get(local_name)
                if rule_id is None:
                    continue
                replacement = by_id[rule_id].replacement.rsplit(".", 1)[1]
                if alias.asname is None:
                    # "from time import clock" becomes "... perf_counter as clock".
                    replacement += f" as {local_name}"
                start = offsets.at(alias.lineno, alias.col_offset)
                edits[rule_id].append(
                    Edit(start, start + len(alias.name), replacement)
                )

    if needs_path_import:
        # Add just one import, and only when a supported path edit exists.
        insertion = _import_position(tree, offsets)
        edits["path-exists"].append(
            Edit(insertion, insertion, _import_text(source, insertion, "pathlib", "Path", path_alias))
        )
    occurrences.sort(key=lambda item: (item.line, item.column, item.rule))
    return occurrences, edits


def _import_position(tree: ast.Module, offsets: SourceOffsets) -> int:
    """Choose an insertion index after the docstring and future imports.

    These statements have special placement semantics. Insert before the first
    ordinary statement, including its decorators, rather than breaking their
    required adjacency. An otherwise exhausted module uses the source end.
    """
    body = tree.body
    index = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            index = 1
    while index < len(body):
        node = body[index]
        if not isinstance(node, ast.ImportFrom) or node.module != "__future__":
            break
        index += 1
    if index < len(body):
        node = body[index]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.decorator_list:
                first = node.decorator_list[0]
                # Decorator expression positions start after the leading '@'.
                return offsets.at(first.lineno, first.col_offset) - 1
        return offsets.span(node)[0]
    return offsets.starts[-1]


def _import_text(source: str, position: int, module: str, name: str, alias: str) -> str:
    """Build an aliased import using the source's newline convention.

    When insertion follows another statement on the same line, first add a line
    break so the new import starts separately. Existing source stays untouched.
    """
    newline = "\r\n" if "\r\n" in source else "\n"
    if "\r" in source and "\n" not in source:
        newline = "\r"
    prefix = newline if position > 0 and source[position - 1] not in "\r\n" else ""
    return f"{prefix}from {module} import {name} as {alias}{newline}"


def rewrite(source: str, edits: list[Edit]) -> str:
    """Apply original-coordinate edits right to left, rejecting overlaps.

    Replacing later text first leaves earlier offsets valid even when replacement
    lengths differ. Zero-width insertions can share a boundary with replacements.
    This function only builds text; the pipeline checks syntax and semantics
    limited to selected-rule matching before writing the result.
    """
    result = source
    previous_start = len(source) + 1
    for edit in sorted(edits, key=lambda item: (item.start, item.end), reverse=True):
        if edit.end > previous_start:
            raise ValueError("Overlapping migration edits")
        result = result[: edit.start] + edit.replacement + result[edit.end :]
        previous_start = edit.start
    return result
