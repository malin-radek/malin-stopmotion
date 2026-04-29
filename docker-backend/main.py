"""
Stop-Motion Backend - FastAPI

Backend zarzadza kontami, projektami, parowaniem telefonow, synchronizacja
materialu z aplikacji mobilnej oraz renderem filmu. Stare endpointy MVP
(/stream, /frame/take, /frames) zostaja jako kompatybilna warstwa dla GUI.
"""

import asyncio
import base64
import io
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import cv2
import httpx
import numpy as np
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


PHONE_IP = os.getenv("PHONE_IP", "192.168.1.100")
PHONE_PORT = int(os.getenv("PHONE_PORT", "8081"))
PHONE_STREAM_URL = f"http://{PHONE_IP}:{PHONE_PORT}/stream"
PHONE_FRAME_URL = f"http://{PHONE_IP}:{PHONE_PORT}/frame"

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = Path(os.getenv("PROJECTS_DIR", BASE_DIR / "projects"))
ACCOUNTS_FILE = PROJECTS_DIR / "accounts.json"
PAIRINGS_FILE = PROJECTS_DIR / "pairings.json"
STATIC_DIR = BASE_DIR / "static"

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

latest_jpeg: Optional[bytes] = None
current_project: Optional[str] = None

app = FastAPI(title="StopMotion Backend", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    fps: int = Field(default=12, ge=1, le=60)
    resolution: str = "1920x1080"
    orientation: str = Field(default="landscape", pattern="^(landscape|portrait)$")
    local_id: Optional[str] = None


class ConfigUpdate(BaseModel):
    phone_ip: str
    phone_port: int = 8081


class PairClaim(BaseModel):
    token: str
    device_name: str = "Telefon"
    device_id: Optional[str] = None


class FrameSync(BaseModel):
    local_id: str
    jpeg_base64: Optional[str] = None
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    deleted: bool = False


class ProjectSync(BaseModel):
    local_id: str
    server_id: Optional[str] = None
    name: str
    fps: int = 12
    resolution: str = "1920x1080"
    orientation: str = "landscape"
    frames: list[FrameSync] = Field(default_factory=list)


class MobileSync(BaseModel):
    account_id: str
    device_id: str
    projects: list[ProjectSync] = Field(default_factory=list)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_. -]+", "", value).strip().replace(" ", "-")
    return value[:80] or "item"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _accounts() -> dict[str, Any]:
    data = _read_json(ACCOUNTS_FILE, {"accounts": []})
    if not data.get("accounts"):
        account = {
            "id": "default",
            "name": "Default",
            "created_at": _now_ms(),
            "devices": [],
        }
        data["accounts"] = [account]
        _write_json(ACCOUNTS_FILE, data)
    return data


def _get_account(account_id: str) -> dict[str, Any]:
    account = next((a for a in _accounts()["accounts"] if a["id"] == account_id), None)
    if not account:
        raise HTTPException(404, "Konto nie istnieje")
    return account


def _save_account(account: dict[str, Any]) -> None:
    data = _accounts()
    data["accounts"] = [account if a["id"] == account["id"] else a for a in data["accounts"]]
    _write_json(ACCOUNTS_FILE, data)


def _account_dir(account_id: str) -> Path:
    return PROJECTS_DIR / _slug(account_id)


def _project_dir(account_id: str, project_id: str) -> Path:
    return _account_dir(account_id) / _slug(project_id)


def _manifest_path(account_id: str, project_id: str) -> Path:
    return _project_dir(account_id, project_id) / "manifest.json"


def _load_manifest(account_id: str, project_id: str) -> dict[str, Any]:
    path = _manifest_path(account_id, project_id)
    if not path.exists():
        raise HTTPException(404, "Projekt nie istnieje")
    return _read_json(path, {})


def _save_manifest(account_id: str, project_id: str, manifest: dict[str, Any]) -> None:
    _write_json(_manifest_path(account_id, project_id), manifest)


def _project_exists(account_id: str, project_id: str) -> bool:
    return _manifest_path(account_id, project_id).exists()


def _create_project(account_id: str, payload: ProjectCreate) -> dict[str, Any]:
    _get_account(account_id)
    base_id = _slug(payload.local_id or payload.name)
    project_id = base_id
    suffix = 2
    while _project_exists(account_id, project_id):
        existing = _load_manifest(account_id, project_id)
        if payload.local_id and existing.get("local_id") == payload.local_id:
            return existing
        project_id = f"{base_id}-{suffix}"
        suffix += 1

    project_dir = _project_dir(account_id, project_id)
    (project_dir / "frames").mkdir(parents=True, exist_ok=True)
    (project_dir / "thumbs").mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": project_id,
        "local_id": payload.local_id,
        "account_id": account_id,
        "project": payload.name,
        "fps": payload.fps,
        "resolution": payload.resolution,
        "orientation": payload.orientation,
        "created_at": _now_ms(),
        "updated_at": _now_ms(),
        "frames": [],
        "deleted_frame_ids": [],
    }
    _save_manifest(account_id, project_id, manifest)
    return manifest


def _list_projects(account_id: str) -> list[dict[str, Any]]:
    root = _account_dir(account_id)
    if not root.exists():
        return []
    projects = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        data = _read_json(manifest_path, {})
        frames = data.get("frames", [])
        projects.append({
            "id": data.get("id", manifest_path.parent.name),
            "name": data.get("project", manifest_path.parent.name),
            "frames": len(frames),
            "fps": data.get("fps", 12),
            "resolution": data.get("resolution", "1920x1080"),
            "orientation": data.get("orientation", "landscape"),
            "updated_at": data.get("updated_at", 0),
        })
    return projects


def _legacy_project_id(project: str) -> str:
    if not _project_exists("default", project):
        _create_project("default", ProjectCreate(name=project, local_id=project))
    return project


def _black_jpeg(w: int, h: int) -> bytes:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def _make_thumbnail(jpeg: bytes, size=(256, 144)) -> bytes:
    arr = np.frombuffer(jpeg, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return _black_jpeg(*size)
    thumb = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 72])
    return buf.tobytes()


def _store_frame(
    account_id: str,
    project_id: str,
    jpeg: bytes,
    local_id: Optional[str] = None,
    timestamp: Optional[int] = None,
    source: str = "backend",
) -> dict[str, Any]:
    manifest = _load_manifest(account_id, project_id)
    if local_id:
        existing = next((f for f in manifest.get("frames", []) if f.get("local_id") == local_id), None)
        if existing:
            return existing

    frame_id = str(uuid.uuid4())[:12]
    timestamp = timestamp or _now_ms()
    filename = f"{timestamp}_{frame_id}.jpg"
    thumbname = f"{timestamp}_{frame_id}.jpg"
    project_dir = _project_dir(account_id, project_id)
    (project_dir / "frames").mkdir(parents=True, exist_ok=True)
    (project_dir / "thumbs").mkdir(parents=True, exist_ok=True)
    (project_dir / "frames" / filename).write_bytes(jpeg)
    (project_dir / "thumbs" / thumbname).write_bytes(_make_thumbnail(jpeg))

    frame = {
        "id": frame_id,
        "local_id": local_id,
        "filename": filename,
        "thumbnail": thumbname,
        "timestamp": timestamp,
        "source": source,
    }
    manifest.setdefault("frames", []).append(frame)
    manifest["updated_at"] = _now_ms()
    _save_manifest(account_id, project_id, manifest)
    return frame


def _delete_from_frame(account_id: str, project_id: str, frame_id: str) -> dict[str, Any]:
    manifest = _load_manifest(account_id, project_id)
    frames = manifest.get("frames", [])
    idx = next((i for i, frame in enumerate(frames) if frame["id"] == frame_id or frame.get("local_id") == frame_id), -1)
    if idx < 0:
        raise HTTPException(404, "Klatka nie znaleziona")
    removed = frames[idx:]
    for frame in removed:
        for folder, key in (("frames", "filename"), ("thumbs", "thumbnail")):
            file_path = _project_dir(account_id, project_id) / folder / frame.get(key, "")
            if file_path.exists():
                file_path.unlink()
        if frame.get("local_id"):
            manifest.setdefault("deleted_frame_ids", []).append(frame["local_id"])
    manifest["frames"] = frames[:idx]
    manifest["updated_at"] = _now_ms()
    _save_manifest(account_id, project_id, manifest)
    return {"deleted": [f["id"] for f in removed], "remaining": len(manifest["frames"])}


def _delete_single_frame(account_id: str, project_id: str, frame_id: str) -> dict[str, Any]:
    manifest = _load_manifest(account_id, project_id)
    frames = manifest.get("frames", [])
    target = next((f for f in frames if f["id"] == frame_id or f.get("local_id") == frame_id), None)
    if not target:
        raise HTTPException(404, "Klatka nie znaleziona")
    for folder, key in (("frames", "filename"), ("thumbs", "thumbnail")):
        file_path = _project_dir(account_id, project_id) / folder / target.get(key, "")
        if file_path.exists():
            file_path.unlink()
    if target.get("local_id"):
        manifest.setdefault("deleted_frame_ids", []).append(target["local_id"])
    manifest["frames"] = [f for f in frames if f is not target]
    manifest["updated_at"] = _now_ms()
    _save_manifest(account_id, project_id, manifest)
    return {"deleted": target["id"], "remaining": len(manifest["frames"])}


async def phone_frame_puller():
    global latest_jpeg
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                async with client.stream("GET", PHONE_STREAM_URL) as resp:
                    buffer = b""
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        buffer += chunk
                        start = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9")
                        if start != -1 and end != -1 and end > start:
                            latest_jpeg = buffer[start : end + 2]
                            buffer = buffer[end + 2 :]
        except Exception as exc:
            print(f"[puller] Blad polaczenia z telefonem: {exc}. Retry za 3s...")
            await asyncio.sleep(3)


@app.on_event("startup")
async def startup_event():
    _accounts()
    asyncio.create_task(phone_frame_puller())


@app.get("/", response_class=HTMLResponse)
def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return index.read_text(encoding="utf-8")
    return "<h1>StopMotion Backend</h1><p>GUI nie zostalo skopiowane do obrazu.</p>"


@app.get("/health")
def health():
    return {"status": "ok", "phone": PHONE_STREAM_URL}


@app.get("/config")
def get_config():
    return {"phone_ip": PHONE_IP, "phone_port": PHONE_PORT, "current_project": current_project}


@app.post("/config")
def update_config(cfg: ConfigUpdate):
    global PHONE_IP, PHONE_PORT, PHONE_STREAM_URL, PHONE_FRAME_URL
    PHONE_IP = cfg.phone_ip
    PHONE_PORT = cfg.phone_port
    PHONE_STREAM_URL = f"http://{PHONE_IP}:{PHONE_PORT}/stream"
    PHONE_FRAME_URL = f"http://{PHONE_IP}:{PHONE_PORT}/frame"
    return {"ok": True, "stream": PHONE_STREAM_URL}


@app.get("/accounts")
def list_accounts():
    return _accounts()


@app.post("/accounts")
def create_account(payload: AccountCreate):
    data = _accounts()
    account_id = _slug(payload.name).lower()
    base_id = account_id
    suffix = 2
    existing_ids = {a["id"] for a in data["accounts"]}
    while account_id in existing_ids:
        account_id = f"{base_id}-{suffix}"
        suffix += 1
    account = {"id": account_id, "name": payload.name, "created_at": _now_ms(), "devices": []}
    data["accounts"].append(account)
    _write_json(ACCOUNTS_FILE, data)
    return account


@app.post("/accounts/{account_id}/pairing")
def create_pairing(account_id: str):
    account = _get_account(account_id)
    token = str(uuid.uuid4())
    pairings = _read_json(PAIRINGS_FILE, {"pairings": []})
    pairings["pairings"].append({
        "token": token,
        "account_id": account_id,
        "account_name": account["name"],
        "created_at": _now_ms(),
        "expires_at": _now_ms() + 15 * 60 * 1000,
        "claimed": False,
    })
    _write_json(PAIRINGS_FILE, pairings)
    payload = f"stopmotion://pair?token={token}"
    return {
        "token": token,
        "pairing_payload": payload,
        "qr_url": f"/pairings/{token}/qr.svg",
        "expires_in_seconds": 900,
    }


@app.get("/pairings/{token}/qr.svg")
def pairing_qr(token: str):
    payload = f"stopmotion://pair?token={token}"
    factory = qrcode.image.svg.SvgImage
    img = qrcode.make(payload, image_factory=factory, box_size=12)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.post("/pairings/claim")
def claim_pairing(payload: PairClaim):
    pairings = _read_json(PAIRINGS_FILE, {"pairings": []})
    pairing = next((p for p in pairings["pairings"] if p["token"] == payload.token), None)
    if not pairing or pairing.get("claimed") or pairing.get("expires_at", 0) < _now_ms():
        raise HTTPException(404, "Kod parowania jest nieprawidlowy albo wygasl")
    account = _get_account(pairing["account_id"])
    device_id = payload.device_id or str(uuid.uuid4())
    if not any(d["id"] == device_id for d in account.get("devices", [])):
        account.setdefault("devices", []).append({
            "id": device_id,
            "name": payload.device_name,
            "paired_at": _now_ms(),
            "last_seen": _now_ms(),
        })
    _save_account(account)
    pairing["claimed"] = True
    _write_json(PAIRINGS_FILE, pairings)
    return {"account": account, "device_id": device_id, "projects": _list_projects(account["id"])}


@app.get("/accounts/{account_id}/projects")
def account_projects(account_id: str):
    _get_account(account_id)
    return {"projects": _list_projects(account_id)}


@app.post("/accounts/{account_id}/projects")
def create_project(account_id: str, payload: ProjectCreate):
    return _create_project(account_id, payload)


@app.get("/projects/{project_id}")
def get_project(project_id: str, account_id: str = "default"):
    return _load_manifest(account_id, project_id)


@app.patch("/projects/{project_id}")
def update_project(project_id: str, payload: ProjectCreate, account_id: str = "default"):
    manifest = _load_manifest(account_id, project_id)
    manifest.update({
        "project": payload.name,
        "fps": payload.fps,
        "resolution": payload.resolution,
        "orientation": payload.orientation,
        "updated_at": _now_ms(),
    })
    _save_manifest(account_id, project_id, manifest)
    return manifest


async def generate_mjpeg() -> AsyncIterator[bytes]:
    while True:
        frame = latest_jpeg or _black_jpeg(640, 480)
        header = (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
        )
        yield header + frame + b"\r\n"
        await asyncio.sleep(0.033)


@app.get("/stream")
async def stream():
    return StreamingResponse(
        generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/frame/take")
async def take_frame(project: str = "default", account_id: str = "default"):
    global current_project
    current_project = project
    frame = latest_jpeg
    if frame is None:
        raise HTTPException(503, "Brak klatki z telefonu")
    project_id = _legacy_project_id(project) if account_id == "default" else project
    stored = _store_frame(account_id, project_id, frame, source="backend-live")
    thumb_b64 = base64.b64encode(_make_thumbnail(frame, size=(160, 120))).decode()
    return {
        "frame_id": stored["id"],
        "filename": stored["filename"],
        "project": project_id,
        "thumbnail": f"data:image/jpeg;base64,{thumb_b64}",
    }


@app.get("/frames")
def list_frames(project: str = "default", account_id: str = "default"):
    project_id = _legacy_project_id(project) if account_id == "default" else project
    manifest = _load_manifest(account_id, project_id)
    return manifest


@app.delete("/frames/{frame_id}")
def delete_frame(frame_id: str, project: str = "default", account_id: str = "default"):
    project_id = _legacy_project_id(project) if account_id == "default" else project
    return _delete_single_frame(account_id, project_id, frame_id)


@app.delete("/frames/from/{frame_id}")
def delete_from(frame_id: str, project: str, account_id: str = "default"):
    return _delete_from_frame(account_id, project, frame_id)


@app.get("/frame/{frame_id}.jpg")
def get_frame_image(frame_id: str, project: str = "default", account_id: str = "default"):
    project_id = _legacy_project_id(project) if account_id == "default" else project
    manifest = _load_manifest(account_id, project_id)
    target = next((f for f in manifest.get("frames", []) if f["id"] == frame_id), None)
    if not target:
        raise HTTPException(404, "Klatka nie znaleziona")
    frame_file = _project_dir(account_id, project_id) / "frames" / target["filename"]
    return Response(content=frame_file.read_bytes(), media_type="image/jpeg")


@app.get("/frame/{frame_id}/thumb.jpg")
def get_frame_thumb(frame_id: str, project: str, account_id: str = "default"):
    manifest = _load_manifest(account_id, project)
    target = next((f for f in manifest.get("frames", []) if f["id"] == frame_id), None)
    if not target:
        raise HTTPException(404, "Klatka nie znaleziona")
    frame_file = _project_dir(account_id, project) / "thumbs" / target.get("thumbnail", "")
    return Response(content=frame_file.read_bytes(), media_type="image/jpeg")


@app.get("/projects")
def list_projects(account_id: Optional[str] = None):
    if account_id:
        return {"projects": _list_projects(account_id)}

    projects = []
    for account in _accounts()["accounts"]:
        for project in _list_projects(account["id"]):
            project["account_id"] = account["id"]
            project["account_name"] = account["name"]
            projects.append(project)
    return {"projects": projects}


@app.post("/projects/{project_id}/fps")
def set_fps(project_id: str, fps: int, account_id: str = "default"):
    manifest = _load_manifest(account_id, project_id)
    manifest["fps"] = fps
    manifest["updated_at"] = _now_ms()
    _save_manifest(account_id, project_id, manifest)
    return {"fps": fps}


@app.post("/mobile/sync")
def mobile_sync(payload: MobileSync):
    account = _get_account(payload.account_id)
    for device in account.get("devices", []):
        if device["id"] == payload.device_id:
            device["last_seen"] = _now_ms()
    _save_account(account)

    project_map = []
    for incoming in payload.projects:
        project_id = incoming.server_id
        if not project_id or not _project_exists(payload.account_id, project_id):
            project = _create_project(
                payload.account_id,
                ProjectCreate(
                    name=incoming.name,
                    fps=incoming.fps,
                    resolution=incoming.resolution,
                    orientation=incoming.orientation,
                    local_id=incoming.local_id,
                ),
            )
            project_id = project["id"]
        else:
            project = _load_manifest(payload.account_id, project_id)
            project.update({
                "project": incoming.name,
                "fps": incoming.fps,
                "resolution": incoming.resolution,
                "orientation": incoming.orientation,
                "updated_at": _now_ms(),
            })
            _save_manifest(payload.account_id, project_id, project)

        for frame in incoming.frames:
            if frame.deleted:
                try:
                    _delete_single_frame(payload.account_id, project_id, frame.local_id)
                except HTTPException:
                    pass
                continue
            if not frame.jpeg_base64:
                continue
            try:
                jpeg = base64.b64decode(frame.jpeg_base64)
            except Exception:
                continue
            _store_frame(
                payload.account_id,
                project_id,
                jpeg,
                local_id=frame.local_id,
                timestamp=frame.timestamp,
                source=f"phone:{payload.device_id}",
            )

        manifest = _load_manifest(payload.account_id, project_id)
        response_frames = []
        for stored in manifest.get("frames", []):
            thumb_path = _project_dir(payload.account_id, project_id) / "thumbs" / stored.get("thumbnail", "")
            response_frames.append({
                "id": stored["id"],
                "local_id": stored.get("local_id"),
                "timestamp": stored.get("timestamp"),
                "thumbnail_base64": base64.b64encode(thumb_path.read_bytes()).decode() if thumb_path.exists() else None,
            })
        project_map.append({
            "local_id": incoming.local_id,
            "server_id": project_id,
            "frames": response_frames,
            "deleted_frame_ids": manifest.get("deleted_frame_ids", []),
        })

    return {"ok": True, "projects": project_map, "server_time": _now_ms()}


@app.post("/projects/{project_id}/render")
def render_project(project_id: str, account_id: str = "default"):
    manifest = _load_manifest(account_id, project_id)
    frames = manifest.get("frames", [])
    if not frames:
        raise HTTPException(400, "Projekt nie ma klatek")

    output_dir = _project_dir(account_id, project_id) / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{project_id}_{_now_ms()}.mp4"
    list_file = output_dir / "frames.txt"
    frame_paths = [_project_dir(account_id, project_id) / "frames" / f["filename"] for f in frames]
    list_file.write_text(
        "\n".join(f"file '{path.as_posix()}'" for path in frame_paths),
        encoding="utf-8",
    )
    fps = int(manifest.get("fps", 12))
    cmd = [
        "ffmpeg",
        "-y",
        "-r",
        str(fps),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-vf",
        "format=yuv420p",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(500, result.stderr[-1000:])
    return {"ok": True, "download_url": f"/projects/{project_id}/exports/{output.name}?account_id={account_id}"}


@app.get("/projects/{project_id}/exports/{filename}")
def download_export(project_id: str, filename: str, account_id: str = "default"):
    path = _project_dir(account_id, project_id) / "exports" / filename
    if not path.exists():
        raise HTTPException(404, "Eksport nie istnieje")
    return FileResponse(path, media_type="video/mp4", filename=filename)
