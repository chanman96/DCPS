"""
FastAPI application — telematics gateway + dashboard host.
"""
from __future__ import annotations
import asyncio
import os, signal
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .simulator import FleetSimulator
from .faults import FAULT_LABELS
from .presets import ECU_PRESETS, PRESET_ORDER


STATIC_DIR = Path(__file__).parent / "static"
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _find_sibling_run_processes(exclude_pid: int) -> list[int]:
    """Find PIDs of other `python run.py` processes for THIS project (matched by resolved
    project root, not just the literal string "run.py" — that alone would risk matching an
    unrelated project's script of the same name). Best-effort: if `ps`/`lsof` aren't
    available the caller just falls back to killing its own process, same as before.

    This exists because "Stop simulation" only ever killed the one process that received
    the click — a second instance started from an IDE or a stray terminal kept running,
    kept ticking its own sim loop, and kept sending alert emails, with no visible link back
    to the browser tab someone had just used to "stop" everything.
    """
    pids: list[int] = []
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="],
                              capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return pids
    for line in out.splitlines():
        line = line.strip()
        if not line or "run.py" not in line:
            continue
        pid_str, _, cmd = line.partition(" ")
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == exclude_pid:
            continue
        if PROJECT_ROOT in cmd:
            pids.append(pid)
            continue
        # Launched as a bare "python run.py" from inside the project dir — the command
        # line has no path in it, so fall back to checking the process's working directory.
        try:
            cwd_out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                      capture_output=True, text=True, timeout=3).stdout
            cwd = next((l[1:] for l in cwd_out.splitlines() if l.startswith("n")), None)
            if cwd == PROJECT_ROOT:
                pids.append(pid)
        except Exception:
            pass
    return pids


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

class PresetAction(BaseModel):
    vehicle_id: str
    preset: str

class EmailConfig(BaseModel):
    smtp_host: str | None = None      # e.g. "smtp.gmail.com"
    smtp_port: int | None = None      # e.g. 587
    smtp_user: str | None = None      # e.g. "you@gmail.com"
    smtp_password: str | None = None  # e.g. a Google "App Password"
    to_addr: str | None = None        # where alerts are delivered
    min_severity: str | None = None   # "warn" | "critical"


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

    @app.get("/api/presets")
    async def presets_list():
        return {"presets": [
            {"key": k, "label": ECU_PRESETS[k]["label"], "note": ECU_PRESETS[k]["note"]}
            for k in PRESET_ORDER
        ]}

    @app.post("/api/preset")
    async def preset_apply(action: PresetAction):
        ok = sim.apply_preset(action.vehicle_id, action.preset)
        if not ok: raise HTTPException(400, "invalid vehicle or preset")
        return {"ok": True}

    @app.get("/api/alerts")
    async def alerts():
        return {"alerts": sim.analyzer.all_alerts()}

    @app.post("/api/stop")
    async def stop_server():
        """Stop the simulator and terminate every FleetTune run.py process for this
        project — not just the one serving this request — shortly after responding.

        The small delay ensures the HTTP response can be sent back to the browser before
        any process receives a termination signal.
        """
        sim.stop()
        my_pid = os.getpid()
        async def _killer():
            await asyncio.sleep(0.5)
            # Deal with sibling processes fully *before* signaling self — once this
            # process gets its own SIGTERM, uvicorn's shutdown can tear down the event
            # loop at any point, so anything scheduled after that isn't guaranteed to run.
            sibling_pids = _find_sibling_run_processes(exclude_pid=my_pid)
            for pid in sibling_pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if sibling_pids:
                await asyncio.sleep(1.0)
                for pid in sibling_pids:
                    try:
                        os.kill(pid, signal.SIGKILL)   # some instances don't respond to SIGTERM promptly
                    except ProcessLookupError:
                        pass
            os.kill(my_pid, signal.SIGTERM)
            # Uvicorn's graceful shutdown doesn't always exit promptly on SIGTERM alone
            # (observed hanging past several seconds) — force it if it's still around.
            await asyncio.sleep(1.0)
            try:
                os.kill(my_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # schedule background task to terminate all processes
        asyncio.create_task(_killer())
        return {"ok": True, "message": "shutting down all FleetTune processes"}

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
                if ts <= 0 or ts > 50:
                    raise HTTPException(400, "time_scale out of range")
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
        path = sim.enable_logging()
        return {"ok": True, "path": str(path)}

    @app.post("/api/admin/logging/stop")
    async def logging_stop():
        sim.disable_logging()
        return {"ok": True}

    # ----- Admin API (email alert delivery) -----
    @app.get("/api/admin/email/status")
    async def email_status():
        return sim.notifier.status()

    @app.post("/api/admin/email/configure")
    async def email_configure(cfg: EmailConfig):
        if cfg.min_severity is not None and cfg.min_severity not in ("warn", "critical"):
            raise HTTPException(400, "min_severity must be 'warn' or 'critical'")
        sim.notifier.configure(**cfg.model_dump(exclude_unset=True))
        return {"ok": True, "status": sim.notifier.status()}

    @app.post("/api/admin/email/test")
    async def email_test():
        # send_test() makes a blocking SMTP call — run off the event loop so it can't
        # stall the 10 Hz sim tick loop for the duration of the request.
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, sim.notifier.send_test)

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
