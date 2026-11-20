"""Start the Alpha Finance Radar HTTP service."""
import argparse
import sys

import uvicorn
from core.config import get_config


def main(application="app:app"):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=config.get("app.host", "127.0.0.1"), help="Address to listen on")
    parser.add_argument("--port", type=int, default=config.get("app.port", 9001),
                        help="HTTP port (default: %(default)s)")
    args = parser.parse_args()
    uvicorn.run(application, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
