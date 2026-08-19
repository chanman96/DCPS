#!/usr/bin/env python3
"""
FleetTune launcher.

Usage:
  python run.py                    # 8 vehicles, port 8000
  python run.py --vehicles 15
  python run.py --vehicles 3 --port 9000 --time-scale 5
"""
import argparse
import signal
import sys
import uvicorn

from fleettune.simulator import FleetSimulator
from fleettune.main import create_app


def main():
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

    print(f"\n  FleetTune  ·  {args.vehicles} vehicle(s)  ·  http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
