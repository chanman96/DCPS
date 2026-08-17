"""
FastAPI application — telematics gateway + dashboard host.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .simulator import FleetSimulator
from .faults import FAULT_LABELS


STATIC_DIR = Path(__file__).parent / "static"


# Simulator lives on app.state — configured by run.py before startup
def get_simulator(app: FastAPI) -> FleetSimulator:
    return app.state.simulator


# ---------- Pydantic control models ----------

class FaultAction(BaseModel):
    vehicle_id: str
    kind: str

class IgnitionAction(BaseModel):
    vehicle_id: str
    on: bool

class TuneParams(BaseModel):
    vehicle_id: str
    idle_rpm_target: float | None = None
    rev_limit_rpm: float | None = None
    speed_governor_kph: float | None = None
    fuel_trim_pct: float | None = None

class MapUpdate(BaseModel):
    vehicle_id: str
    which: str    # "fuel" | "timing" | "boost"
    matrix: list[list[float]]


# ---------- App factory ----------

def create_app(sim: FleetSimulator) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(sim.run())
        try:
            yield
        finally:
            sim.stop()
            task.cancel()

    app = FastAPI(title="FleetTune", lifespan=lifespan)
    app.state.simulator = sim

    # ----- Static & index -----
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC_DIR / "index.html").read_text()

    # ----- REST: fleet directory -----
    @app.get("/api/fleet")
    async def fleet():
        return {"vehicles": sim.list_vehicles(),
                "fault_types": [{"kind": k, "label": v} for k, v in FAULT_LABELS.items()]}

    @app.get("/api/vehicle/{vid}/maps")
    async def maps(vid: str):
        m = sim.get_maps(vid)
        if not m: raise HTTPException(404, "unknown vehicle")
        return m

    # ----- REST: control -----
    @app.post("/api/fault/inject")
    async def inject(action: FaultAction):
        ok = sim.inject_fault(action.vehicle_id, action.kind)
        if not ok: raise HTTPException(400, "invalid vehicle or fault kind")
        return {"ok": True}

    @app.post("/api/fault/clear")
    async def clear(action: FaultAction):
        ok = sim.clear_fault(action.vehicle_id, action.kind)
        if not ok: raise HTTPException(400, "invalid vehicle or fault kind")
        return {"ok": True}

    @app.post("/api/ignition")
    async def ignition(a: IgnitionAction):
        ok = sim.set_ignition(a.vehicle_id, a.on)
        if not ok: raise HTTPException(400, "invalid vehicle")
        return {"ok": True}

    @app.post("/api/tune")
    async def tune(params: TuneParams):
        d = params.model_dump(exclude_unset=True)
        vid = d.pop("vehicle_id")
        ok = sim.apply_tune(vid, d)
        if not ok: raise HTTPException(400, "invalid vehicle")
        return {"ok": True, "applied": d}

    @app.post("/api/map")
    async def map_update(u: MapUpdate):
        ok = sim.apply_map(u.vehicle_id, u.which, u.matrix)
        if not ok: raise HTTPException(400, "invalid vehicle or map name")
        return {"ok": True}

    @app.get("/api/alerts")
    async def alerts():
        return {"alerts": sim.analyzer.all_alerts()}

    # ----- WebSockets -----
    @app.websocket("/ws/telemetry")
    async def ws_telemetry(ws: WebSocket):
        await ws.accept()
        q = sim.subscribe_telemetry()
        try:
            # Send fleet directory on connect
            await ws.send_json({"type": "hello", "vehicles": sim.list_vehicles(),
                                "fault_types": [{"kind": k, "label": v} for k, v in FAULT_LABELS.items()]})
            while True:
                msg = await q.get()
                await ws.send_json(msg)
        except WebSocketDisconnect:
            pass
        finally:
            sim.unsubscribe_telemetry(q)

    @app.websocket("/ws/frames")
    async def ws_frames(ws: WebSocket):
        await ws.accept()
        q = sim.subscribe_frames()
        # Optional filter query: ?vehicle_id=TRK-001
        filter_vid: str | None = ws.query_params.get("vehicle_id")
        try:
            while True:
                msg = await q.get()
                if filter_vid and msg["vehicle_id"] != filter_vid:
                    continue
                await ws.send_json(msg)
        except WebSocketDisconnect:
            pass
        finally:
            sim.unsubscribe_frames(q)

    return app
