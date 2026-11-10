"""Offline Kokoro voice server for MoneyPrinterTurbo (no Docker, no cloud TTS).

Serves the small OpenAI-compatible API that the built-in Kokoro voice provider
expects, on http://127.0.0.1:8880/v1. It uses kokoro-onnx, which runs on CPU.

    ./claude-local.sh --local-voice      # starts this server together with the app
    # or on its own:
    uv run --with kokoro-onnx==0.6.1 --with soundfile python scripts/kokoro_local_server.py

On the first run it downloads the model files (about 350 MB) from the
kokoro-onnx GitHub release into storage/kokoro/. After that it works offline.
Then pick a "kokoro:..." voice in the WebUI (for example kokoro:af_heart).
"""


import argparse
import io
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.environ.get("KOKORO_MODEL_DIR", ROOT / "storage" / "kokoro"))
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def ensure_models() -> tuple[Path, Path]:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        target = MODEL_DIR / name
        if target.exists() and target.stat().st_size > 0:
            continue
        print(f"Downloading {name} (first run only)...", flush=True)
        partial = target.with_suffix(target.suffix + ".part")
        urllib.request.urlretrieve(f"{RELEASE}/{name}", partial)
        partial.replace(target)
    return MODEL_DIR / FILES[0], MODEL_DIR / FILES[1]


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def to_audio(samples, sample_rate: int, fmt: str) -> tuple[bytes, str]:
    import soundfile as sf

    wav = io.BytesIO()
    sf.write(wav, samples, sample_rate, format="WAV")
    if fmt == "wav":
        return wav.getvalue(), "audio/wav"
    mp3 = subprocess.run(
        [_ffmpeg(), "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
         "-codec:a", "libmp3lame", "-q:a", "2", "-f", "mp3", "pipe:1"],
        input=wav.getvalue(), capture_output=True, check=True,
    ).stdout
    return mp3, "audio/mpeg"


def build_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import Response
    from kokoro_onnx import Kokoro
    from pydantic import BaseModel

    model_path, voices_path = ensure_models()
    kokoro = Kokoro(str(model_path), str(voices_path))
    voices = sorted(kokoro.get_voices())

    class SpeechRequest(BaseModel):
        input: str
        voice: str = "af_heart"
        model: str = "kokoro"
        response_format: str = "mp3"
        speed: float = 1.0

    app = FastAPI(title="Local Kokoro TTS")

    @app.get("/v1/audio/voices")
    def list_voices():
        return {"voices": voices}

    @app.post("/v1/audio/speech")
    def speech(req: SpeechRequest):
        voice = req.voice.removeprefix("kokoro:")
        if voice not in voices:
            raise HTTPException(400, f"unknown voice '{voice}'")
        lang = "en-gb" if voice.startswith("b") else "en-us"
        samples, rate = kokoro.create(
            req.input, voice=voice, speed=max(0.5, min(2.0, req.speed)), lang=lang
        )
        body, media_type = to_audio(samples, rate, req.response_format.lower())
        return Response(content=body, media_type=media_type)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Kokoro TTS server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8880)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
