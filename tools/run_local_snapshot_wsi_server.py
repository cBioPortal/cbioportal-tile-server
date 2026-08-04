#!/usr/bin/env python3
"""Run the local WSI tile server against the trusted resource index."""

from __future__ import annotations

import argparse
import os

import uvicorn

from app.main import app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Bind host.")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SERVER_PORT", "8081")),
        help="Bind port.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
