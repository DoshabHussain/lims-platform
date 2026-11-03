"""Launcher script for the LabSentinel LIMS Web UI and REST API.

Run:
    python run_ui.py
or
    python run_ui.py --port 8000
"""
import argparse
import sys
import webbrowser

sys.path.insert(0, "src")

import uvicorn
from lims_core.server import app

def main():
    parser = argparse.ArgumentParser(description="LabSentinel LIMS Server & UI")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"============================================================")
    print(f"🚀 LabSentinel LIMS Dashboard & REST API is starting!")
    print(f"📍 Web UI URL: {url}")
    print(f"📖 Interactive API Docs: {url}/docs")
    print(f"============================================================")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()

