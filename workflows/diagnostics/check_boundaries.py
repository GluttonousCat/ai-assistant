"""Dependency ratchet: forbid new upward imports and direct API/Agent SQL access.

Existing debt is recorded per module/import/count, never as a whole-directory
exemption. Remove or reduce baseline entries when migrating the corresponding
code. This checker requires no database, config, network or model.
"""
from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path

from core.paths import project_root

ROOT = project_root()
BASELINE = ROOT / "docs" / "ops" / "dependency_baseline.json"
FORBIDDEN = {
    "core": {"api", "harness", "skills", "tools", "storage", "jobs", "workflows"},
    "storage": {"api", "harness", "skills", "tools", "jobs", "workflows"},
    "tools": {"api", "harness", "skills", "jobs", "workflows"},
    "harness": {"api", "storage", "jobs", "workflows"},
    "skills": {"api", "storage", "jobs", "workflows"},
    "api": {"storage", "jobs", "workflows"},
}


def violations(root: Path = ROOT) -> Counter:
    found = Counter()
    for layer, forbidden in FORBIDDEN.items():
        for path in (root / layer).rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [n.name for n in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    modules = [node.module]
                for module in modules:
                    if module.split(".")[0] in forbidden:
                        found[f"{path.relative_to(root).as_posix()} -> {module}"] += 1
    return found


def _baseline() -> Counter:
    return Counter(json.loads(BASELINE.read_text(encoding="utf-8")))


def new_violations(root: Path = ROOT) -> Counter:
    return violations(root) - _baseline()


def main() -> int:
    found = violations()
    new = found - _baseline()
    if new:
        print(json.dumps(dict(new), ensure_ascii=False, indent=2))
        return 1
    print(f"No new dependency violations; {sum(found.values())} legacy imports tracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
