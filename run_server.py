"""Start the Alpha Finance Radar HTTP service."""
import argparse

import uvicorn


def main(application="app:app"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Address to listen on")
    parser.add_argument("--port", type=int, default=8208, help="HTTP port (default: 8208)")
    args = parser.parse_args()
    uvicorn.run(application, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
