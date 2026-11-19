import io
import json
import logging
import sys

from harness.mcp import server


def test_stdio_keeps_preexisting_logging_and_tool_prints_off_protocol_stream(
    monkeypatch,
):
    protocol, diagnostics = io.StringIO(), io.StringIO()
    logger = logging.getLogger("mcp_stdio_regression")
    handler = logging.StreamHandler(protocol)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    monkeypatch.setattr(
        sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    )
    monkeypatch.setattr(sys, "stdout", protocol)
    monkeypatch.setattr(sys, "stderr", diagnostics)

    def handle(message):
        logger.info("database diagnostic")
        print("incidental tool output")
        return json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}})

    monkeypatch.setattr(server, "handle", handle)
    try:
        server.main()
        lines = protocol.getvalue().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["id"] == 1
        assert "database diagnostic" in diagnostics.getvalue()
        assert "incidental tool output" in diagnostics.getvalue()
    finally:
        logger.removeHandler(handler)
        handler.close()
