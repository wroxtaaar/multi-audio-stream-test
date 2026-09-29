import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
HLS_ROOT = Path(os.getenv("HLS_ROOT", "/tmp/multi-audio-hls"))
MEDIA_SOURCE_URL = os.getenv("MEDIA_SOURCE_URL", "").strip()
SEGMENT_SECONDS = max(2, int(os.getenv("HLS_SEGMENT_SECONDS", "4")))
MAX_AUDIO_TRACKS = max(1, int(os.getenv("MAX_AUDIO_TRACKS", "8")))
STARTUP_TIMEOUT = max(10, int(os.getenv("STARTUP_TIMEOUT", "45")))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("multi-audio")

app = FastAPI(title="Multi Audio Stream Test")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

_probe_cache: dict[str, Any] | None = None
_ffmpeg: asyncio.subprocess.Process | None = None
_ffmpeg_log_task: asyncio.Task | None = None
_started_at: float | None = None
_lock = asyncio.Lock()
_smoke_test_result: dict[str, Any] | None = None
_smoke_test_task: asyncio.Task | None = None


async def direct_source_smoke_check() -> dict[str, Any]:
    """Verify the direct Seedr URL from inside the Render container without downloading the file."""
    parsed = urlparse(MEDIA_SOURCE_URL)
    started = time.monotonic()
    result: dict[str, Any] = {
        "host": parsed.hostname,
        "path": parsed.path,
    }
    try:
        req = Request(
            MEDIA_SOURCE_URL,
            headers={
                "User-Agent": "Mozilla/5.0 multi-audio-stream-test/1.0",
                "Range": "bytes=0-65535",
                "Accept": "*/*",
            },
            method="GET",
        )
        with urlopen(req, timeout=20) as response:
            result.update({
                "status": int(response.status),
                "finalHost": urlparse(response.geturl()).hostname,
                "contentType": response.headers.get("Content-Type"),
                "contentLength": response.headers.get("Content-Length"),
                "contentRange": response.headers.get("Content-Range"),
                "acceptRanges": response.headers.get("Accept-Ranges"),
                "sampleBytes": len(response.read(65536)),
            })
    except HTTPError as exc:
        result.update({"status": exc.code, "error": f"HTTP {exc.code}: {exc.reason}"})
    except (URLError, TimeoutError, OSError) as exc:
        result["error"] = f"Network error: {exc}"
    except Exception as exc:
        result["error"] = f"Unexpected error: {type(exc).__name__}: {exc}"
    result["elapsedMs"] = round((time.monotonic() - started) * 1000)
    log.info("Direct source check: %s", json.dumps(result, sort_keys=True))
    return result


async def smoke_test() -> None:
    global _smoke_test_result
    started = time.monotonic()
    try:
        source = await direct_source_smoke_check()
        if "status" not in source or source.get("status", 0) >= 400:
            _smoke_test_result = {"ok": False, "stage": "http", "source": source}
            return

        info = await probe(force=True)
        log.info(
            "Smoke ffprobe: video=%s audio=%d duration=%s",
            info["video"]["codec"], info["audioTrackCount"], info["duration"],
        )
        if not info["audioTracks"]:
            _smoke_test_result = {"ok": False, "stage": "audio", "source": source, "probe": info}
            return

        await stop_ffmpeg(remove_files=True)
        HLS_ROOT.mkdir(parents=True, exist_ok=True)
        cmd = build_ffmpeg(info)
        log.info("Smoke HLS: launching FFmpeg with %d audio track(s)", info["audioTrackCount"])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=HLS_ROOT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + min(25, STARTUP_TIMEOUT)
            while time.monotonic() < deadline:
                if proc.returncode is not None:
                    _, err = await proc.communicate()
                    _smoke_test_result = {
                        "ok": False,
                        "stage": "hls-process",
                        "source": source,
                        "probe": info,
                        "exit": proc.returncode,
                        "stderr": err.decode("utf-8", "replace")[-2500:],
                    }
                    return
                master = HLS_ROOT / "master.m3u8"
                if master.exists() and master.stat().st_size:
                    text = master.read_text("utf-8", errors="replace")
                    audio_lines = [
                        line for line in text.splitlines()
                        if line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line
                    ]
                    uri_count = sum(1 for line in audio_lines if "URI=" in line)
                    _smoke_test_result = {
                        "ok": len(audio_lines) == info["audioTrackCount"] and uri_count == info["audioTrackCount"],
                        "stage": "hls",
                        "source": source,
                        "probe": info,
                        "audioRenditions": len(audio_lines),
                        "audioPlaylistUris": uri_count,
                        "master": text[:6000],
                    }
                    log.info(
                        "Smoke HLS result: ok=%s renditions=%d uriCount=%d",
                        _smoke_test_result["ok"], len(audio_lines), uri_count,
                    )
                    return
                await asyncio.sleep(0.25)
            _smoke_test_result = {"ok": False, "stage": "hls-timeout", "source": source, "probe": info}
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except Exception:
                    proc.kill()
                    await proc.wait()
            shutil.rmtree(HLS_ROOT, ignore_errors=True)
    except HTTPException as exc:
        _smoke_test_result = {"ok": False, "stage": "probe", "status": exc.status_code, "detail": str(exc.detail)}
        log.error("Smoke test failed: %s", _smoke_test_result)
    except Exception as exc:
        _smoke_test_result = {"ok": False, "stage": "unexpected", "error": f"{type(exc).__name__}: {exc}"}
        log.exception("Smoke test failed unexpectedly")
    finally:
        log.info("Smoke test finished in %d ms", round((time.monotonic() - started) * 1000))


def label_for(language: str, index: int) -> str:
    aliases = {"eng": "English", "hin": "Hindi", "spa": "Spanish", "fra": "French", "fre": "French"}
    language = (language or "").strip()
    return aliases.get(language.lower(), language.upper()) if language else f"Audio {index + 1}"


async def run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(504, f"Command timed out after {timeout}s")
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def probe(force: bool = False) -> dict[str, Any]:
    global _probe_cache
    if _probe_cache is not None and not force:
        return _probe_cache
    if not MEDIA_SOURCE_URL.startswith(("http://", "https://")):
        raise HTTPException(500, "MEDIA_SOURCE_URL is not configured as an HTTP(S) URL")

    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,codec_long_name,channels,sample_rate:stream_tags=language,title:stream_disposition=default",
        "-of", "json", MEDIA_SOURCE_URL,
    ]
    code, out, err = await run_command(cmd, 35)
    if code != 0:
        raise HTTPException(502, f"Could not inspect Seedr source: {(err or out)[-1600:]}")

    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "ffprobe returned invalid JSON") from exc

    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"][:MAX_AUDIO_TRACKS]
    if not videos:
        raise HTTPException(422, "The source contains no video stream")

    tracks = []
    for pos, stream in enumerate(audios):
        tags = stream.get("tags") or {}
        disp = stream.get("disposition") or {}
        lang = str(tags.get("language") or "")
        title = str(tags.get("title") or "").strip()
        tracks.append({
            "position": pos,
            "inputIndex": int(stream["index"]),
            "title": title or label_for(lang, pos),
            "language": lang,
            "codec": str(stream.get("codec_name") or ""),
            "codecLong": str(stream.get("codec_long_name") or ""),
            "channels": int(stream.get("channels") or 0),
            "sampleRate": int(stream.get("sample_rate") or 0),
            "default": bool(disp.get("default")),
        })

    raw_duration = (data.get("format") or {}).get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None

    _probe_cache = {
        "video": {
            "inputIndex": int(videos[0]["index"]),
            "codec": str(videos[0].get("codec_name") or ""),
            "codecLong": str(videos[0].get("codec_long_name") or ""),
        },
        "audioTracks": tracks,
        "audioTrackCount": len(tracks),
        "duration": duration,
    }
    log.info("Probe: video=%s audio=%d duration=%s", _probe_cache["video"]["codec"], len(tracks), duration)
    return _probe_cache


def build_ffmpeg(info: dict[str, Any]) -> list[str]:
    tracks = info["audioTracks"]
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", MEDIA_SOURCE_URL, "-map", "0:v:0",
    ]
    for track in tracks:
        cmd += ["-map", f"0:{track['inputIndex']}"]

    cmd += ["-c:v", "copy"]
    for i, track in enumerate(tracks):
        if track["codec"] == "aac":
            cmd += [f"-c:a:{i}", "copy"]
        else:
            bitrate = "384k" if track["channels"] >= 6 else "160k"
            cmd += [f"-c:a:{i}", "aac", f"-b:a:{i}", bitrate, f"-ar:a:{i}", "48000"]
    cmd += ["-avoid_negative_ts", "make_zero"]

    variants = []
    has_default = any(track["default"] for track in tracks)
    for i, track in enumerate(tracks):
        default = "yes" if (track["default"] or (not has_default and i == 0)) else "no"
        item = [f"a:{i}", "agroup:audio", f"default:{default}", f"name:audio_{i}"]
        language = re.sub(r"[^A-Za-z0-9-]", "", track["language"] or "")
        if language:
            item.append(f"language:{language}")
        variants.append(",".join(item))
    variants.append("v:0,agroup:audio,name:video")

    cmd += [
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_playlist_type", "event",
        "-hls_list_size", "0",
        "-hls_segment_type", "fmp4",
        "-hls_flags", "independent_segments",
        "-master_pl_name", "master.m3u8",
        "-hls_fmp4_init_filename", "%v_init.mp4",
        "-hls_segment_filename", "%v_seg_%06d.m4s",
        "-var_stream_map", " ".join(variants),
        "%v.m3u8",
    ]
    return cmd


async def drain_ffmpeg(stderr: asyncio.StreamReader) -> None:
    while True:
        line = await stderr.readline()
        if not line:
            return
        message = line.decode("utf-8", "replace").strip()
        if message:
            log.warning("ffmpeg: %s", message[-1200:])


async def stop_ffmpeg(remove_files: bool = False) -> None:
    global _ffmpeg, _ffmpeg_log_task, _started_at
    if _ffmpeg is not None and _ffmpeg.returncode is None:
        try:
            _ffmpeg.terminate()
            await asyncio.wait_for(_ffmpeg.wait(), 3)
        except Exception:
            try:
                _ffmpeg.kill()
                await _ffmpeg.wait()
            except Exception:
                pass
    if _ffmpeg_log_task is not None and not _ffmpeg_log_task.done():
        _ffmpeg_log_task.cancel()
    _ffmpeg = None
    _ffmpeg_log_task = None
    _started_at = None
    if remove_files:
        shutil.rmtree(HLS_ROOT, ignore_errors=True)


async def ensure_hls() -> dict[str, Any]:
    global _ffmpeg, _ffmpeg_log_task, _started_at
    async with _lock:
        master = HLS_ROOT / "master.m3u8"
        if _ffmpeg is not None and _ffmpeg.returncode is None and master.exists():
            return await probe()

        info = await probe()
        if len(info["audioTracks"]) < 2:
            log.warning("Source contains only %d audio track(s)", len(info["audioTracks"]))

        await stop_ffmpeg(remove_files=True)
        HLS_ROOT.mkdir(parents=True, exist_ok=True)

        cmd = build_ffmpeg(info)
        log.info("Starting FFmpeg: %d audio tracks", len(info["audioTracks"]))
        _ffmpeg = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=HLS_ROOT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _started_at = time.time()
        _ffmpeg_log_task = asyncio.create_task(drain_ffmpeg(_ffmpeg.stderr))

        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if master.exists() and master.stat().st_size:
                return info
            if _ffmpeg.returncode is not None:
                raise HTTPException(502, f"FFmpeg stopped before HLS was ready (exit={_ffmpeg.returncode})")
            await asyncio.sleep(0.25)

        raise HTTPException(504, f"HLS did not become ready within {STARTUP_TIMEOUT}s")


def safe_file(path: str) -> Path:
    target = (HLS_ROOT / path.lstrip("/")).resolve()
    root = HLS_ROOT.resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "Invalid HLS path")
    return target


def mime_for(path: Path) -> str:
    return {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".m4s": "video/iso.segment",
        ".mp4": "video/mp4",
    }.get(path.suffix.lower(), "application/octet-stream")


def rewrite_master(text: str, tracks: list[dict[str, Any]]) -> str:
    for i, track in enumerate(tracks):
        label = re.sub(r"[^A-Za-z0-9 _.-]+", " ", track["title"]).strip() or f"Audio {i + 1}"
        text = re.sub(
            rf'(TYPE=AUDIO,[^\n]*NAME=")audio_{i}("[^\n]*)',
            rf'\1{label}\2',
            text,
            count=1,
        )
    return text


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/info")
async def api_info() -> dict[str, Any]:
    info = await probe()
    return {
        **info,
        "hlsReady": (HLS_ROOT / "master.m3u8").exists(),
        "ffmpegRunning": bool(_ffmpeg is not None and _ffmpeg.returncode is None),
        "startedAt": _started_at,
        "segmentSeconds": SEGMENT_SECONDS,
        "smokeTest": _smoke_test_result,
    }


@app.get("/api/self-test")
async def api_self_test() -> dict[str, Any]:
    """Run the integration smoke test on-demand from the Render instance."""
    await smoke_test()
    return _smoke_test_result or {"ok": False, "stage": "unknown"}


@app.post("/api/start")
async def api_start() -> dict[str, Any]:
    info = await ensure_hls()
    return {**info, "hlsUrl": "/api/hls/master.m3u8"}


@app.post("/api/reset")
async def api_reset() -> dict[str, bool]:
    global _probe_cache
    await stop_ffmpeg(remove_files=True)
    _probe_cache = None
    return {"ok": True}


@app.get("/api/hls/{path:path}")
async def api_hls(path: str):
    if not (HLS_ROOT / "master.m3u8").exists():
        await ensure_hls()

    target = safe_file(path)
    if not target.is_file():
        raise HTTPException(404, "HLS resource not ready")

    headers = {
        "Cache-Control": "no-store, max-age=0",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Range",
        "X-Content-Type-Options": "nosniff",
    }
    if target.name == "master.m3u8":
        text = target.read_text("utf-8", errors="replace")
        text = rewrite_master(text, (await probe())["audioTracks"])
        return Response(text, media_type="application/vnd.apple.mpegurl", headers=headers)
    return FileResponse(target, media_type=mime_for(target), headers=headers)


@app.on_event("startup")
async def startup() -> None:
    global _smoke_test_task
    if os.getenv("RUN_STARTUP_SMOKE_TEST", "false").lower() in {"1", "true", "yes"}:
        _smoke_test_task = asyncio.create_task(smoke_test())


@app.on_event("shutdown")
async def shutdown() -> None:
    await stop_ffmpeg(remove_files=False)
