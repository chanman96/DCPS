"""
FastAPI application — telematics gateway + dashboard host.
"""
from __future__ import annotations
import asyncio
import os, signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
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
        # Use simulator.start() so admin can re-start later if needed
        try:
            sim.start()
            yield
        finally:
            sim.stop()
            # cancel background task if present
            if getattr(sim, "_task", None):
                try:
                    sim._task.cancel()
                except Exception:
                    pass

    app = FastAPI(title="FleetTune", lifespan=lifespan)
    app.state.simulator = sim

    # ----- Static & index -----
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC_DIR / "index.html").read_text()

    # Admin UI (separate control center)
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_index():
        return (STATIC_DIR / "admin.html").read_text()

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

    @app.get("/api/vehicle/{vid}/tune-events")
    async def tune_events(vid: str):
        return {"events": sim.get_tune_events(vid)}

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

    @app.post("/api/stop")
    async def stop_server():
        """Stop the simulator and terminate the running process shortly after responding.

        The small delay ensures the HTTP response can be sent back to the browser before
        the process receives SIGTERM.
        """
        sim.stop()
        async def _killer():
            await asyncio.sleep(0.5)
            # send the termination signal to the current process
            os.kill(os.getpid(), signal.SIGTERM)
        # schedule background task to terminate process
        asyncio.create_task(_killer())
        return {"ok": True, "message": "shutting down"}

    # ----- Admin API (control center) -----
    @app.get("/api/admin/config")
    async def admin_config():
        return sim.get_config()

    @app.post("/api/admin/reconfigure")
    async def admin_reconfigure(payload: dict):
        # Accept keys: n_vehicles, time_scale
        n = payload.get("n_vehicles")
        ts = payload.get("time_scale")
        if n is None and ts is None:
            raise HTTPException(400, "nothing to change")
        try:
            if n is not None:
                n = int(n)
                if n < 1 or n > 200:
                    raise HTTPException(400, "n_vehicles out of range")
            if ts is not None:
                ts = float(ts)
        except ValueError:
            raise HTTPException(400, "invalid parameter types")
        sim.reconfigure(n_vehicles=n, time_scale=ts)
        return {"ok": True, "config": sim.get_config()}

    @app.post("/api/admin/start")
    async def admin_start():
        started = sim.start()
        return {"ok": True, "started": started}

    @app.post("/api/admin/stop")
    async def admin_stop():
        sim.stop()
        return {"ok": True}

    # ----- Admin API (Excel telemetry logging) -----
    @app.get("/api/admin/logging/status")
    async def logging_status():
        return sim.excel_logger.status()

    @app.post("/api/admin/logging/start")
    async def logging_start():
        path = sim.excel_logger.start()
        return {"ok": True, "path": str(path)}

    @app.post("/api/admin/logging/stop")
    async def logging_stop():
        sim.excel_logger.stop()
        return {"ok": True}

    @app.get("/api/admin/logging/download")
    async def logging_download():
        path = sim.excel_logger.flush()
        if not path or not path.exists():
            raise HTTPException(404, "no telemetry log yet")
        return FileResponse(
            path, filename=path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

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
