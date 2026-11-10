"""Photoreal visuals for episodic series via Google's Gemini API.

* Portraits: one photoreal full-body reference photo per character, made with a
  Gemini image model from the bible's ``look`` text.
* Scenes: one live-action clip per scene from Gemini Omni Flash (the video
  model that replaced Veo on the Gemini API in Oct 2026). Up to two characters
  per clip are pinned to their portrait as reference images; the clip's own
  audio carries the spoken dialogue with lip-sync.

Needs a Gemini API key: ``GEMINI_API_KEY`` in the environment, or
``gemini_api_key`` in config.toml. Billing must be enabled on the key.
Approximate cost (Nov 2026): ~$0.07 per second of 720p video, so a 64 s
episode is about $4.50.
"""

import base64
import os
import subprocess
import time
from pathlib import Path

import requests
from loguru import logger

API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_VIDEO_MODEL = "gemini-omni-1.2-flash"
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
MAX_REF_CHARACTERS = 2  # Omni accepts reference images for up to two characters
WORDS_PER_SECOND = 2.4


class RealisticError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        from app.config import config

        key = str(config.app.get("gemini_api_key", "") or "").strip()
    if not key:
        raise RealisticError(
            "No Gemini API key. Set GEMINI_API_KEY or gemini_api_key in config.toml."
        )
    return key


def _post(path: str, body: dict, timeout: int) -> dict:
    last = None
    for attempt in range(3):
        try:
            resp = requests.post(f"{API}/{path}", headers={"x-goog-api-key": api_key()},
                                 json=body, timeout=timeout)
        except requests.RequestException as exc:
            last = str(exc)
        else:
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}: {resp.text[:400]}"
            if resp.status_code < 500 and resp.status_code != 429:
                break
        time.sleep(5 * (attempt + 1))
    raise RealisticError(f"Gemini API call failed ({path}): {last}")


def make_portrait(character: dict, out: Path, model: str = DEFAULT_IMAGE_MODEL) -> Path:
    prompt = (
        f"Photorealistic full-body character reference photo of a fictional adult: "
        f"{character['name']}, {character.get('role', '')} "
        f"Appearance: {character.get('look', '')} "
        "Standing facing the camera, neutral relaxed expression, plain light-grey studio "
        "background, soft even lighting, sharp focus, natural skin texture, 35mm photo. "
        "Not a real or famous person. No text."
    )
    data = _post(
        f"models/{model}:generateContent",
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16"}}},
        timeout=180,
    )
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            out.write_bytes(base64.b64decode(part["inlineData"]["data"]))
            return out
    raise RealisticError(f"no image returned for {character['name']}")


def clip_seconds(scene: dict, bounds=(5, 15)) -> int:
    """Clip length from the spoken words, kept inside the bible's shot_seconds (Omni: 3-15 s)."""
    low, high = max(3, int(bounds[0])), min(15, int(bounds[-1]))
    words = sum(len(line["text"].split()) for line in scene.get("lines", []))
    needed = round(words / WORDS_PER_SECOND + 1.5)
    if needed > high:  # never cut dialogue short: stretch past the pacing target (max 15 s)
        logger.warning(f"shot {scene.get('id')} needs {needed}s for its lines; pacing target is {high}s")
        return int(min(15, needed))
    return int(max(low, needed))


def scene_prompt(bible: dict, scene: dict, ref_ids: list[str], seconds: int) -> str:
    chars = bible.get("characters", {})
    setting = bible.get("setting", {})
    who = []
    for i, cid in enumerate(ref_ids):
        c = chars[cid]
        who.append(f"{c['name']} is the person in <IMAGE_REF_{i}>: keep their face, hair and outfit exactly.")
    for cid in scene.get("characters", []):
        if cid not in ref_ids:
            c = chars[cid]
            who.append(f"{c['name']}: {c.get('look', '')}")
    dialogue = []
    for line in scene.get("lines", []):
        c = chars[line["speaker"]]
        style = c.get("voice_style", "natural")
        dialogue.append(f"{c['name']} ({style}): \"{line['text']}\"")
    return "\n".join([
        f"Photorealistic cinematic live-action shot for a vertical TV drama, 9:16, about {seconds} seconds.",
        "Continuous shot, no scene cuts, no on-screen text, captions or subtitles, no background music.",
        f"Setting: {setting.get('island', '')} {setting.get('camp', '')}".strip(),
        "Characters:",
        *who,
        f"Shot: {scene['shot']}",
        "Dialogue, spoken clearly in this order with natural lip-sync, nothing else spoken:" if dialogue else "No dialogue.",
        *dialogue,
        "Sound design: natural ambience for the location (surf, wind, birds or rain as fits the shot).",
    ])


def make_clip(bible: dict, scene: dict, portraits: dict, out: Path,
              model: str = DEFAULT_VIDEO_MODEL, resolution: str = "720p") -> Path:
    # Reference slots are scarce (2): the protagonist first whenever they are in
    # frame, then whoever speaks, then anyone else in the shot.
    lead = bible.get("protagonist")
    speakers = [line["speaker"] for line in scene.get("lines", [])]
    in_frame = scene.get("characters", [])
    order = list(dict.fromkeys(([lead] if lead in in_frame else []) + speakers + in_frame))
    ref_ids = [cid for cid in order if cid in portraits][:MAX_REF_CHARACTERS]
    seconds = clip_seconds(scene, (bible.get("format") or {}).get("shot_seconds", (5, 15)))
    items = [{"type": "text", "text": scene_prompt(bible, scene, ref_ids, seconds)}]
    for cid in ref_ids:
        items.append({"type": "image", "mime_type": "image/png",
                      "data": base64.b64encode(Path(portraits[cid]).read_bytes()).decode()})
    body = {"model": model, "input": items, "store": False,
            "response_format": {"type": "video", "aspect_ratio": "9:16",
                                "resolution": resolution, "duration": f"{seconds}s"}}
    logger.info(f"omni clip {scene['id']}: {seconds}s, refs={ref_ids}")
    data = _post("interactions", body, timeout=900)

    def find_video(node):
        if isinstance(node, dict):
            if node.get("type") == "video" and node.get("data"):
                return node["data"]
            for value in node.values():
                found = find_video(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = find_video(value)
                if found:
                    return found
        return None

    video_b64 = find_video(data)
    if not video_b64:
        raise RealisticError(f"no video in the reply for scene {scene['id']} (status {data.get('status')})")
    out.write_bytes(base64.b64decode(video_b64))
    return out


def probe_seconds(ffmpeg: str, path: Path) -> float:
    info = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True).stderr
    for token in info.split("Duration:")[1:2]:
        h, m, s = token.split(",")[0].strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    raise RealisticError(f"cannot read duration of {path}")


def normalise_clip(ffmpeg: str, src: Path, out: Path, width: int, height: int, fps: int) -> None:
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
         "-vf", f"scale={width}:{height}:flags=lanczos,fps={fps},format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
         "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k", str(out)],
        check=True,
    )


def still_prompt(bible: dict, scene: dict, ref_ids: list[str]) -> str:
    chars = bible.get("characters", {})
    setting = bible.get("setting", {})
    who = [f"{chars[cid]['name']} is the person in reference image {i + 1}: keep their face, hair, "
           f"body and outfit exactly." for i, cid in enumerate(ref_ids)]
    for cid in scene.get("characters", []):
        if cid not in ref_ids:
            who.append(f"{chars[cid]['name']}: {chars[cid].get('look', '')}")
    speaking = ", ".join(dict.fromkeys(chars[l["speaker"]]["name"] for l in scene.get("lines", [])))
    return "\n".join([
        "Photorealistic cinematic film still from a live-action TV drama, vertical 9:16 frame.",
        "Natural light, shallow depth of field, real skin texture, candid mid-action moment.",
        "No text, captions, logos or watermarks. Keep the lower third free of faces (subtitles go there).",
        f"Setting: {setting.get('island', '')} {setting.get('camp', '')}".strip(),
        "Characters (fictional adults):",
        *who,
        f"Shot: {scene['shot']}",
        f"{speaking} is speaking in this moment; show it in their expression." if speaking else "",
    ])


def make_still(bible: dict, scene: dict, portraits: dict, out: Path,
               model: str = DEFAULT_IMAGE_MODEL, max_refs: int = 4) -> Path:
    """One photoreal frame for a shot, with the cast portraits as identity references."""
    lead = bible.get("protagonist")
    speakers = [line["speaker"] for line in scene.get("lines", [])]
    in_frame = scene.get("characters", [])
    order = list(dict.fromkeys(([lead] if lead in in_frame else []) + speakers + in_frame))
    ref_ids = [cid for cid in order if cid in portraits and cid in in_frame][:max_refs]
    parts = [{"text": still_prompt(bible, scene, ref_ids)}]
    for cid in ref_ids:
        parts.append({"inlineData": {"mimeType": "image/png",
                                     "data": base64.b64encode(Path(portraits[cid]).read_bytes()).decode()}})
    data = _post(
        f"models/{model}:generateContent",
        {"contents": [{"parts": parts}],
         "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16"}}},
        timeout=180,
    )
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            out.write_bytes(base64.b64decode(part["inlineData"]["data"]))
            return out
    raise RealisticError(f"no image returned for shot {scene.get('id')}")
