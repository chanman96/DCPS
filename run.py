#!/usr/bin/env python3
"""
FleetTune launcher.

Usage:
  python run.py                    # 8 vehicles, port 8000
  python run.py --vehicles 15
  python run.py --vehicles 3 --port 9000 --time-scale 5
"""
import argparse
import os
import threading
import webbrowser
from pathlib import Path

import uvicorn

from fleettune.simulator import FleetSimulator
from fleettune.main import create_app


def _load_env_file(path: str = ".env"):
    """Load KEY=VALUE lines from a local .env file into os.environ, if present. Lets
    secrets (e.g. SMTP credentials for email alerts) persist across restarts without
    re-entering them in the admin panel each time — .env is gitignored, never committed."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main():
    _load_env_file()
    parser = argparse.ArgumentParser(description="FleetTune simulator")
    parser.add_argument("--vehicles", type=int, default=8,
                        help="Number of simulated trucks (default 8)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--time-scale", type=float, default=1.0,
                        help=">1 speeds the sim up; useful for demoing predictive alerts fast")
    args = parser.parse_args()

    if args.vehicles < 1 or args.vehicles > 100:
        parser.error("--vehicles must be between 1 and 100")

    sim = FleetSimulator(n_vehicles=args.vehicles, time_scale=args.time_scale)
    app = create_app(sim)

    # 0.0.0.0 means "listen on every interface", not a browsable address — open localhost instead.
    browser_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{browser_host}:{args.port}"
    admin_url = f"{url}/admin"
    print(f"\n  FleetTune  ·  {args.vehicles} vehicle(s)  ·  {url}\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    threading.Timer(1.3, lambda: webbrowser.open_new_tab(admin_url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
