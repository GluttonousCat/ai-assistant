"""Fail when new code increases the explicitly tracked legacy dependency debt."""

import ast
import re

from workflows.diagnostics.check_boundaries import new_violations


def test_no_new_upward_imports_or_agent_storage_access():
    assert not new_violations()


def test_mcp_adapters_no_longer_embed_query_implementations(pytestconfig):
    root = pytestconfig.rootpath / "harness" / "mcp" / "tools"
    assert root.is_dir()
    forbidden = re.compile(
        r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s", re.I
    )
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not forbidden.match(node.value), (path.name, node.lineno)


def test_report_and_industry_domains_do_not_regain_embedded_sql(pytestconfig):
    root = pytestconfig.rootpath
    forbidden = re.compile(
        r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s", re.I
    )
    for name in (
        "skills/report/skill.py",
        "skills/beta_alpha/analysis/chain_analysis.py",
        "core/security.py",
    ):
        tree = ast.parse((root / name).read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not forbidden.match(node.value), (name, node.lineno)
