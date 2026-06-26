"""Launch the aiohttp + React V6 trading dashboard."""
from __future__ import annotations

import argparse
import threading
import webbrowser

from aiohttp import web

from sz002796.web_server import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the 002796.SZ V6 web dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8796)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    web.run_app(create_app(), host=args.host, port=args.port, print=lambda text: print(text))


if __name__ == "__main__":
    main()
