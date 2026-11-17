"""Fail when new code increases the explicitly tracked legacy dependency debt."""

from jobs.check_boundaries import new_violations
import ast
from pathlib import Path
import re


def test_no_new_upward_imports_or_agent_storage_access():
    assert not new_violations()


def test_mcp_adapters_no_longer_embed_query_implementations():
    root = Path(__file__).resolve().parents[1] / "mcp" / "tools"
    forbidden = re.compile(
        r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s", re.I
    )
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not forbidden.match(node.value), (path.name, node.lineno)
