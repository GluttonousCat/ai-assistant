# -*- encoding: utf-8 -*-
"""Trajectory log unit tests: on-disk shape / no-session fallback / disabled no-op (no DB dependency)."""
from __future__ import annotations

import json

from harness import trajectory


def test_open_turn_writes_events(tmp_path, monkeypatch):
    monkeypatch.setattr(trajectory, "_dir", lambda: tmp_path)
    tee = trajectory.open_turn("t-traj", "中际旭创的营收")
    assert callable(tee)
    tee("stage", {"message": "调用工具"})
    tee("tool_call", {"tool": "query_financials", "ok": True})
    tee("delta", {"text": "回答"})

    files = list(tmp_path.glob("t-traj_*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]
    # First line turn_start carries the user input; subsequent lines are one per event, all with ts
    assert lines[0]["event"] == "turn_start"
    assert lines[0]["text"] == "中际旭创的营收"
    assert [l["event"] for l in lines[1:]] == ["stage", "tool_call", "delta"]
    assert all("ts" in l for l in lines)


def test_open_turn_session_none_uses_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(trajectory, "_dir", lambda: tmp_path)
    tee = trajectory.open_turn(None, "cli debug 问题")
    tee("delta", {"text": "ok"})
    assert len(list(tmp_path.glob("cli_*.jsonl"))) == 1


def test_disabled_is_noop(tmp_path, monkeypatch):
    class _Cfg:
        root_dir = str(tmp_path)

        def get(self, key, default=None):
            return False if key == "agent.trajectory.enabled" else default

    monkeypatch.setattr(trajectory, "get_config", lambda: _Cfg())
    monkeypatch.setattr(trajectory, "_dir", lambda: tmp_path)
    tee = trajectory.open_turn("t-off", "问题")
    tee("stage", {})
    assert list(tmp_path.glob("*.jsonl")) == []   # not a single file written
