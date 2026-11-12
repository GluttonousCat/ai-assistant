"""Fail when new code increases the explicitly tracked legacy dependency debt."""
from jobs.check_boundaries import new_violations


def test_no_new_upward_imports_or_agent_storage_access():
    assert not new_violations()
