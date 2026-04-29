"""
Stop-Motion Backend – FastAPI
Proxy'uje strumień MJPEG z telefonu, zarządza klatkami projektu.

Uruchom: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
Env:     PHONE_IP=192.168.1.xx  (lub przez /config)
"""

import asyncio
import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

import cv2
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────────
PHONE_IP = os.getenv("PHONE_IP", "192.168.1.100")
PHONE_PORT = int(os.getenv("PHONE_PORT", "8081"))
PHONE_STREAM_URL = f"http://{PHONE_IP}:{PHONE_PORT}/stream"
PHONE_FRAME_URL = f"http://{PHONE_IP}:{PHONE_PORT}/frame"

PROJECTS_DIR = Path("projects")
PROJECTS_DIR.mkdir(exist_ok=True)

# ── State ────────────────────────────────────────────────────────────────────
latest_jpeg: Optional[bytes] = None  # ostatnia klatka z telefonu (aktualizowana co ~33ms)
current_project: Optional[str] = None

app = FastAPI(title="StopMotion Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Background task: ciągłe pobieranie klatek z telefonu ────────────────────
async def phone_frame_puller():
    """Pobiera klatki z telefonu w tle – aktualizuje latest_jpeg."""
    global latest_jpeg
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                async with client.stream("GET", PHONE_STREAM_URL) as resp:
                    buffer = b""
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        buffer += chunk
                        # Szukaj granic JPEG w strumieniu MJPEG
                        start = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9")
                        if start != -1 and end != -1 and end > start:
                            latest_jpeg = buffer[start : end + 2]
                            buffer = buffer[end + 2 :]
        except Exception as e:
            print(f"[puller] Błąd połączenia z telefonem: {e}. Retry za 3s...")
            await asyncio.sleep(3)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(phone_frame_puller())


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "phone": PHONE_STREAM_URL}


@app.get("/config")
def get_config():
    return {"phone_ip": PHONE_IP, "phone_port": PHONE_PORT, "current_project": current_project}


class ConfigUpdate(BaseModel):
    phone_ip: str
    phone_port: int = 8081


@app.post("/config")
def update_config(cfg: ConfigUpdate):
    global PHONE_IP, PHONE_PORT, PHONE_STREAM_URL, PHONE_FRAME_URL
    PHONE_IP = cfg.phone_ip
    PHONE_PORT = cfg.phone_port
    PHONE_STREAM_URL = f"http://{PHONE_IP}:{PHONE_PORT}/stream"
    PHONE_FRAME_URL = f"http://{PHONE_IP}:{PHONE_PORT}/frame"
    return {"ok": True, "stream": PHONE_STREAM_URL}


# ─── /stream – proxy MJPEG na klientów GUI ──────────────────────────────────

async def generate_mjpeg() -> AsyncIterator[bytes]:
    """Pobiera latest_jpeg i wysyła jako MJPEG stream."""
    boundary = b"frame"
    while True:
        frame = latest_jpeg
        if frame is None:
            # Placeholder: czarna klatka 640x480
            frame = _black_jpeg(640, 480)

        header = (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
        )
        yield header + frame + b"\r\n"
        await asyncio.sleep(0.033)  # ~30fps


@app.get("/stream")
async def stream():
    return StreamingResponse(
        generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ─── /frame/take – zapisuje klatkę do projektu ──────────────────────────────

@app.post("/frame/take")
async def take_frame(project: str = "default"):
    global current_project
    current_project = project

    frame = latest_jpeg
    if frame is None:
        raise HTTPException(503, "Brak klatki z telefonu")

    project_dir = PROJECTS_DIR / project / "frames"
    project_dir.mkdir(parents=True, exist_ok=True)

    frame_id = str(uuid.uuid4())[:8]
    timestamp = int(time.time() * 1000)
    filename = f"{timestamp}_{frame_id}.jpg"
    filepath = project_dir / filename

    filepath.write_bytes(frame)

    # Aktualizuj manifest
    _update_manifest(project, frame_id, filename)

    # Zwróć miniaturkę base64 (opcjonalnie)
    thumb = _make_thumbnail(frame, size=(160, 120))
    import base64
    thumb_b64 = base64.b64encode(thumb).decode()

    return {
        "frame_id": frame_id,
        "filename": filename,
        "project": project,
        "thumbnail": f"data:image/jpeg;base64,{thumb_b64}",
    }


# ─── /frames – lista klatek projektu ────────────────────────────────────────

@app.get("/frames")
def list_frames(project: str = "default"):
    project_dir = PROJECTS_DIR / project
    manifest_path = project_dir / "manifest.json"

    if not manifest_path.exists():
        return {"project": project, "frames": [], "fps": 12}

    manifest = json.loads(manifest_path.read_text())
    return manifest


@app.delete("/frames/{frame_id}")
def delete_frame(frame_id: str, project: str = "default"):
    """Usuwa klatkę z projektu (undo)."""
    project_dir = PROJECTS_DIR / project
    manifest_path = project_dir / "manifest.json"

    if not manifest_path.exists():
        raise HTTPException(404, "Projekt nie istnieje")

    manifest = json.loads(manifest_path.read_text())
    frames = manifest.get("frames", [])

    target = next((f for f in frames if f["id"] == frame_id), None)
    if not target:
        raise HTTPException(404, f"Klatka {frame_id} nie znaleziona")

    # Usuń plik
    frame_file = project_dir / "frames" / target["filename"]
    if frame_file.exists():
        frame_file.unlink()

    manifest["frames"] = [f for f in frames if f["id"] != frame_id]
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return {"deleted": frame_id}


# ─── /frame/{frame_id} – serwuj JPEG klatki ─────────────────────────────────

@app.get("/frame/{frame_id}.jpg")
def get_frame_image(frame_id: str, project: str = "default"):
    project_dir = PROJECTS_DIR / project
    manifest_path = project_dir / "manifest.json"

    if not manifest_path.exists():
        raise HTTPException(404, "Projekt nie istnieje")

    manifest = json.loads(manifest_path.read_text())
    target = next((f for f in manifest["frames"] if f["id"] == frame_id), None)
    if not target:
        raise HTTPException(404, "Klatka nie znaleziona")

    frame_file = project_dir / "frames" / target["filename"]
    return Response(content=frame_file.read_bytes(), media_type="image/jpeg")


# ─── /projects – lista projektów ─────────────────────────────────────────────

@app.get("/projects")
def list_projects():
    projects = []
    for p in PROJECTS_DIR.iterdir():
        if p.is_dir():
            manifest = p / "manifest.json"
            frame_count = 0
            fps = 12
            if manifest.exists():
                data = json.loads(manifest.read_text())
                frame_count = len(data.get("frames", []))
                fps = data.get("fps", 12)
            projects.append({"name": p.name, "frames": frame_count, "fps": fps})
    return {"projects": projects}


@app.post("/projects/{name}/fps")
def set_fps(name: str, fps: int):
    manifest_path = PROJECTS_DIR / name / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "Projekt nie istnieje")
    manifest = json.loads(manifest_path.read_text())
    manifest["fps"] = fps
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"fps": fps}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _update_manifest(project: str, frame_id: str, filename: str):
    project_dir = PROJECTS_DIR / project
    manifest_path = project_dir / "manifest.json"

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {"project": project, "fps": 12, "frames": []}

    manifest["frames"].append({
        "id": frame_id,
        "filename": filename,
        "timestamp": int(time.time() * 1000),
    })
    manifest_path.write_text(json.dumps(manifest, indent=2))


def _make_thumbnail(jpeg: bytes, size=(160, 120)) -> bytes:
    arr = np.frombuffer(jpeg, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    thumb = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes()


def _black_jpeg(w: int, h: int) -> bytes:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()
