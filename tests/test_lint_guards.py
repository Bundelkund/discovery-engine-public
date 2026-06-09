"""Static guard tests — cheap AST checks that run in the normal suite so the
two regression classes found by the 2026-06-09 review can never silently return.

Guard 1 (F1): a synchronous supabase ``.execute()`` called directly inside an
``async def`` blocks the event loop. It MUST be wrapped in
``asyncio.to_thread(...)``. (supabase-py is sync.)

Guard 2 (F4): the active jobs shelf is a runtime config value (the cutover
read-switch). No module under app/ may hardcode the table name ``"jobs"`` or
``"jobs_v2"`` in a ``.table(...)`` call — it must come from settings / self.jobs_table.

These are intentionally simple AST walks, not a ruff plugin: zero new infra, and
they fail the existing ``pytest`` run (hence CI) the moment either pattern reappears.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _py_files():
    return sorted(APP.rglob("*.py"))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parent_of: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_of[child] = node
    return parent_of


def _is_execute_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    )


def _is_to_thread_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    # asyncio.to_thread(...)  or  to_thread(...)
    return (isinstance(f, ast.Attribute) and f.attr == "to_thread") or (
        isinstance(f, ast.Name) and f.id == "to_thread"
    )


def _nearest_func(node, parent_of):
    cur = parent_of.get(node)
    while cur is not None:
        if isinstance(cur, (ast.AsyncFunctionDef, ast.FunctionDef)):
            return cur
        cur = parent_of.get(cur)
    return None


def _wrapped_in_to_thread(node, parent_of, stop_at) -> bool:
    cur = parent_of.get(node)
    while cur is not None and cur is not stop_at:
        if _is_to_thread_call(cur):
            return True
        cur = parent_of.get(cur)
    return False


def test_no_sync_execute_inside_async_def():
    """Every supabase .execute() reached while inside an async def must be wrapped
    in asyncio.to_thread (F1). .execute() in plain `def` helpers is fine — the
    async caller is expected to wrap the whole helper."""
    violations: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parent_of = _parents(tree)
        for node in ast.walk(tree):
            if not _is_execute_call(node):
                continue
            enclosing = _nearest_func(node, parent_of)
            if not isinstance(enclosing, ast.AsyncFunctionDef):
                continue  # sync def — caller wraps it
            if not _wrapped_in_to_thread(node, parent_of, stop_at=enclosing):
                violations.append(
                    f"{path.relative_to(APP.parent)}:{node.lineno} "
                    f"-> bare .execute() in async def '{enclosing.name}'"
                )
    assert not violations, (
        "Synchronous supabase .execute() blocks the event loop — wrap in "
        "asyncio.to_thread(lambda: ...). Offenders:\n  " + "\n  ".join(violations)
    )


def test_no_hardcoded_jobs_shelf_table():
    """No app/ module may pin the shelf table name in .table('jobs'|'jobs_v2');
    the read-switch (settings.jobs_table / self.jobs_table) is the only source (F4)."""
    banned = {"jobs", "jobs_v2"}
    violations: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "table"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in banned
            ):
                violations.append(
                    f"{path.relative_to(APP.parent)}:{node.lineno} "
                    f"-> .table({node.args[0].value!r})"
                )
    assert not violations, (
        "Hardcoded shelf table — use the read-switch instead. Offenders:\n  "
        + "\n  ".join(violations)
    )
