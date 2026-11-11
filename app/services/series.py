"""Episodic AI series: series bible -> episode plan -> scenes, voices, subtitles -> video.

A series lives in ``series/<slug>/bible.yaml`` (cast, looks, voices, art style,
season arc). Generated files go to ``storage/series/<slug>/``.

    python -m app.services.series series/coral-key characters        # one model sheet per character
    python -m app.services.series series/coral-key plan --episode 1   # Claude writes the episode
    python -m app.services.series series/coral-key render --episode 1 # stills, voices, subtitles, video
    python -m app.services.series series/coral-key next               # plan + render the next episode

Text (plans, recaps, scene drawings) goes through the configured LLM provider,
which is the Claude subscription on this fork. Voices go through the regular
TTS layer, so ``kokoro:*`` voices need ``scripts/kokoro_local_server.py``
running (``./claude-local.sh --local-voice`` starts it).
"""

import argparse
import json
import math
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from app.models.schema import VideoParams
from app.services import llm, video, voice

WIDTH, HEIGHT, FPS = 1080, 1920, 30
LINE_GAP_MS = 300
SCENE_GAP_MS = 450
MAX_SUBTITLE_CHARS = 38
LLM_WORKERS = 4


class SeriesError(RuntimeError):
    pass


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _ask(prompt: str) -> str:
    reply = llm._generate_response(prompt)
    if not reply or reply.startswith("Error:"):
        raise SeriesError(f"LLM call failed: {reply[:300] if reply else 'empty reply'}")
    return reply


def _extract_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise SeriesError("the episode plan was not JSON")
    return json.loads(match.group(0))


def _extract_svg(text: str) -> str:
    match = re.search(r"<svg\b.*?</svg>", text, re.S | re.I)
    if not match:
        raise SeriesError("no <svg> in the drawing reply")
    svg = match.group(0)
    if "xmlns=" not in svg[:300]:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return svg


def _svg_to_png(svg: str, png_path: Path, width: int, height: int) -> None:
    import resvg_py

    data = resvg_py.svg_to_bytes(svg_string=svg, width=width, height=height)
    png_path.write_bytes(bytes(data))


@dataclass
class Series:
    root: Path
    bible: dict

    @classmethod
    def load(cls, path: str) -> "Series":
        root = Path(path)
        bible_file = root / "bible.yaml" if root.is_dir() else root
        if not bible_file.is_file():
            raise SeriesError(f"no bible.yaml at {bible_file}")
        return cls(bible_file.parent, yaml.safe_load(bible_file.read_text(encoding="utf-8")))

    @property
    def slug(self) -> str:
        return self.root.name

    @property
    def out(self) -> Path:
        from app.utils import utils

        path = Path(utils.storage_dir("series", create=True)) / self.slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def characters(self) -> dict:
        return self.bible.get("characters") or {}

    def episode_dir(self, number: int) -> Path:
        path = self.out / f"ep{number:02d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def planned_episodes(self) -> list[int]:
        return sorted(
            int(p.parent.name[2:])
            for p in self.out.glob("ep*/episode.json")
            if p.parent.name[2:].isdigit()
        )

    def bible_text(self) -> str:
        return yaml.safe_dump(self.bible, sort_keys=False, allow_unicode=True)

    def sheet_svg(self, char_id: str) -> str:
        path = self.out / "characters" / f"{char_id}.svg"
        return path.read_text(encoding="utf-8") if path.exists() else ""


# --------------------------------------------------------------------------
# Characters
# --------------------------------------------------------------------------


def draw_character_sheets(series: Series, force: bool = False) -> list[Path]:
    """Draw one reference sheet per character; scenes reuse these designs."""
    folder = series.out / "characters"
    folder.mkdir(parents=True, exist_ok=True)
    style = series.bible.get("art_style", "")

    def draw(item):
        char_id, char = item
        svg_path = folder / f"{char_id}.svg"
        if svg_path.exists() and not force:
            return svg_path
        prompt = f"""You are the character designer for an animated vertical-video series.
Draw a character model sheet as ONE standalone SVG.

Art style: {style}
Character: {char.get('name')} - {char.get('role')}
Look (follow every detail and hex colour exactly): {char.get('look')}

Requirements:
- viewBox="0 0 600 1000", width="600" height="1000".
- The character standing full body, facing the viewer, neutral friendly pose, centred.
- Plain flat background #F4EFE6. No text, letters or numbers anywhere.
- Build the character from clearly grouped shapes: <g id="head">, <g id="hair">, <g id="body">, <g id="arms">, <g id="legs">, <g id="accessories">.
- Keep it under 12 KB.
Return only the SVG markup."""
        svg = _extract_svg(_ask(prompt))
        svg_path.write_text(svg, encoding="utf-8")
        _svg_to_png(svg, svg_path.with_suffix(".png"), 600, 1000)
        logger.info(f"character sheet: {svg_path}")
        return svg_path

    with ThreadPoolExecutor(LLM_WORKERS) as pool:
        return list(pool.map(draw, series.characters.items()))


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def plan_episode(series: Series, number: int, force: bool = False) -> dict:
    ep_file = series.episode_dir(number) / "episode.json"
    if ep_file.exists() and not force:
        return json.loads(ep_file.read_text(encoding="utf-8"))

    recaps, last_cliffhanger = [], ""
    for previous in range(1, number):
        prev_file = series.out / f"ep{previous:02d}" / "episode.json"
        if prev_file.exists():
            prev = json.loads(prev_file.read_text(encoding="utf-8"))
            recaps.append(f"Part {previous} \"{prev.get('title')}\": {prev.get('recap')}")
            if previous == number - 1 and prev.get("scenes"):
                from app.services.series_shots import last_beat

                last_cliffhanger = last_beat(prev)
    arc = series.bible.get("season_arc") or []
    arc_beat = arc[number - 1] if 0 < number <= len(arc) else "Continue the story naturally from the recaps."
    fmt = series.bible.get("format", {})
    total = fmt.get("parts", len(arc) or "?")
    shots = fmt.get("shots_per_episode") or fmt.get("scenes_per_episode", [7, 9])
    shot_secs = fmt.get("shot_seconds", [3, 8])
    max_lines = fmt.get("max_lines_per_shot", 3)
    max_words = fmt.get("max_words_per_line", 16)
    char_ids = ", ".join(series.characters)

    prompt = f"""You are the head writer of a fast-paced vertical short-video series for TikTok.
Write part {number} of {total}. Viewers swipe away in under 2 seconds if the opening is slow.

SERIES BIBLE (follow it exactly, including structure and guardrails):
{series.bible_text()}

STORY SO FAR:
{chr(10).join(recaps) if recaps else "This is the first part."}
{f"PREVIOUS PART ENDED ON (pay this off in shots 1-2): {last_cliffhanger}" if last_cliffhanger else ""}

THIS PART'S BEAT: {arc_beat}

Pacing rules:
- {shots[0]}-{shots[-1]} shots. Each shot is one continuous camera shot of {shot_secs[0]}-{shot_secs[-1]} seconds.
- At most {max_lines} spoken lines per shot, at most {max_words} words per line. Some shots can be silent reactions or action.
- {fmt.get('spoken_words_per_episode', [150, 200])} spoken words in total.
- Shot 1 is the hook; the last shot is the cliffhanger (see the bible's structure).
- Vary framing: close-ups for reactions and punchlines, wides to show the island and the problem.
Use only these speaker ids: {char_ids}.

Return ONLY JSON in this exact shape:
{{
  "title": "short punchy title",
  "hook": "the first line a viewer hears",
  "cliffhanger": "the final line or reveal",
  "caption": "TikTok caption: 'Part {number}/{total}', one punchy sentence, 3-5 hashtags",
  "scenes": [
    {{
      "id": "s1",
      "shot": "one-paragraph visual description of this single shot: location, time of day, who is in frame, action, expressions, framing (wide / medium / close-up)",
      "characters": ["leo", "vanessa"],
      "lines": [{{"speaker": "leo", "text": "..."}}]
    }}
  ],
  "recap": "3-4 sentence summary of what happened, including any mystery clue, for writing the next part"
}}"""
    episode = _extract_json(_ask(prompt))
    known = set(series.characters)
    for scene in episode.get("scenes", []):
        scene["characters"] = [c for c in scene.get("characters", []) if c in known]
        for line in scene.get("lines", []):
            if line.get("speaker") not in known:
                raise SeriesError(f"unknown speaker {line.get('speaker')!r} in {scene.get('id')}")
    episode["number"] = number
    episode["total"] = total
    ep_file.write_text(json.dumps(episode, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.success(f"planned episode {number}: {episode.get('title')} -> {ep_file}")
    return episode


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _draw_scene(series: Series, scene: dict, folder: Path, force: bool) -> Path:
    png = folder / f"{scene['id']}.png"
    if png.exists() and not force:
        return png
    refs = []
    for char_id in scene.get("characters", []):
        char = series.characters[char_id]
        sheet = series.sheet_svg(char_id)
        refs.append(
            f"### {char['name']} ({char_id})\nLook: {char.get('look')}\n"
            + (f"Reference design SVG (reuse these exact shapes, proportions and colours; change only pose, expression and scale):\n{sheet}\n" if sheet else "")
        )
    prompt = f"""You are the scene artist for an animated vertical-video series.
Draw this single frame as ONE standalone SVG illustration.

Art style: {series.bible.get('art_style', '')}
Setting: {json.dumps(series.bible.get('setting', {}), ensure_ascii=False)}

Scene: {scene['shot']}

Characters in frame (they must be instantly recognisable as the same characters every episode):
{chr(10).join(refs) if refs else 'No characters, environment only.'}

Requirements:
- viewBox="0 0 1080 1920", width="1080" height="1920", full-bleed background.
- Characters large and readable on a phone; keep the bottom 380 px free of faces (subtitles go there).
- No text, letters, numbers, speech bubbles or logos anywhere.
- Under 30 KB. Return only the SVG markup."""
    last_error = None
    for _ in range(2):
        try:
            svg = _extract_svg(_ask(prompt))
            (folder / f"{scene['id']}.svg").write_text(svg, encoding="utf-8")
            _svg_to_png(svg, png, WIDTH, HEIGHT)
            return png
        except Exception as exc:  # bad SVG: ask once more
            last_error = exc
            logger.warning(f"scene {scene['id']} drawing failed, retrying: {exc}")
    raise SeriesError(f"could not draw scene {scene['id']}: {last_error}")


def _voice_lines(series: Series, episode: dict, folder: Path, force: bool) -> list[dict]:
    """TTS every line; returns timeline entries with start/end in ms."""
    from pydub import AudioSegment

    AudioSegment.converter = _ffmpeg()
    timeline, cursor = [], 0
    audio = AudioSegment.silent(duration=200)
    cursor = 200
    for s_index, scene in enumerate(episode["scenes"]):
        scene_start = cursor
        for l_index, line in enumerate(scene["lines"]):
            char = series.characters[line["speaker"]]
            clip_path = folder / f"{scene['id']}-{l_index + 1}-{line['speaker']}.mp3"
            if force or not clip_path.exists():
                if voice.tts(line["text"], char["voice"], 1.0, str(clip_path)) is None:
                    raise SeriesError(
                        f"TTS failed for {char['name']} ({char['voice']}). For kokoro voices start "
                        "scripts/kokoro_local_server.py (./claude-local.sh --local-voice)."
                    )
            clip = AudioSegment.from_file(clip_path)
            timeline.append({"text": line["text"], "start": cursor, "end": cursor + len(clip)})
            audio += clip
            cursor += len(clip)
            gap = LINE_GAP_MS if l_index < len(scene["lines"]) - 1 else SCENE_GAP_MS
            audio += AudioSegment.silent(duration=gap)
            cursor += gap
        planned_ms = int(float(scene.get("seconds") or 0) * 1000)
        if cursor - scene_start < planned_ms:  # silent shots and short lines keep their planned length
            pad = planned_ms - (cursor - scene_start)
            audio += AudioSegment.silent(duration=pad)
            cursor += pad
        scene["_start_ms"], scene["_end_ms"] = scene_start, cursor
    audio.export(folder / "voice.mp3", format="mp3", bitrate="192k")
    return timeline


def _srt_time(ms: int) -> str:
    h, rem = divmod(int(ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(timeline: list[dict], path: Path) -> None:
    cues = []
    for item in timeline:
        words, chunks, current = item["text"].split(), [], ""
        for word in words:
            if current and len(current) + 1 + len(word) > MAX_SUBTITLE_CHARS:
                chunks.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            chunks.append(current)
        total = sum(len(c) for c in chunks) or 1
        start, span = item["start"], item["end"] - item["start"]
        for chunk in chunks:
            end = start + span * len(chunk) / total
            cues.append((start, end, chunk))
            start = end
    path.write_text(
        "\n".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1)),
        encoding="utf-8",
    )


def _still_to_clip(png: Path, seconds: float, out: Path, zoom_in: bool = True, move: int | None = None) -> None:
    """Animate a still: 0 zoom in, 1 zoom out, 2 pan left, 3 pan right (Ken Burns)."""
    frames = max(1, round(seconds * FPS))
    move = (0 if zoom_in else 1) if move is None else move % 4
    if move in (0, 1):
        zoom = f"1+0.10*on/{frames}" if move == 0 else f"1.10-0.10*on/{frames}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    else:
        zoom = "1.12"
        travel = f"(iw-iw/zoom)*on/{frames}"
        x = f"(iw-iw/zoom)-{travel}" if move == 2 else travel
        y = "ih/2-(ih/zoom/2)"
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(png), "-vf",
         f"scale={WIDTH * 2}:{HEIGHT * 2},zoompan=z='{zoom}':d={frames}:"
         f"x='{x}':y='{y}':s={WIDTH}x{HEIGHT}:fps={FPS},format=yuv420p",
         "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)],
        check=True,
    )


def _render_drawn(series: Series, episode: dict, folder: Path, force: bool) -> Path:
    """Claude-drawn stills + one TTS voice per character. Returns the silent combined video."""
    if any(not series.sheet_svg(c) for c in series.characters):
        draw_character_sheets(series)

    logger.info(f"drawing {len(episode['scenes'])} scenes")
    with ThreadPoolExecutor(LLM_WORKERS) as pool:
        stills = list(pool.map(lambda s: _draw_scene(series, s, folder / "scenes", force), episode["scenes"]))

    logger.info("recording voices")
    timeline = _voice_lines(series, episode, folder / "audio", force)
    _write_srt(timeline, folder / "subtitles.srt")

    logger.info("animating scenes")
    clips = []
    for i, (scene, png) in enumerate(zip(episode["scenes"], stills)):
        clip = folder / "clips" / f"{scene['id']}.mp4"
        _still_to_clip(png, (scene["_end_ms"] - scene["_start_ms"]) / 1000, clip, zoom_in=i % 2 == 0)
        clips.append(clip)
    return _concat(clips, folder / "clips" / "combined.mp4")


def _concat(clips: list[Path], out: Path) -> Path:
    clip_list = out.parent / "list.txt"
    clip_list.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(clip_list),
         "-c", "copy", str(out)],
        check=True,
    )
    return out


def portrait_prompt(character: dict) -> str:
    return (
        f"Photorealistic full-body character reference photo of a fictional adult: "
        f"{character['name']}, {character.get('role', '')} "
        f"Appearance: {character.get('look', '')} "
        "Standing facing the camera, neutral relaxed expression, plain light-grey studio "
        "background, soft even lighting, sharp focus, natural skin texture, 35mm photo. "
        "Not a real or famous person. No text."
    )


def ensure_portraits(series: Series, force: bool = False) -> dict:
    """Photoreal reference portrait per character.

    Made by the ComfyUI workflow in render.comfy.portrait_workflow when that is set
    (free on your own GPU), otherwise by the Gemini image model.
    """
    from app.services import series_realistic as real

    folder = series.out / "characters"
    folder.mkdir(parents=True, exist_ok=True)
    render = series.bible.get("render") or {}
    model = render.get("image_model", real.DEFAULT_IMAGE_MODEL)
    comfy_cfg = render.get("comfy") or {}
    todo = [cid for cid in series.characters if force or not (folder / f"{cid}.photo.png").exists()]

    if todo and comfy_cfg.get("portrait_workflow"):
        from app.services import comfyui_client as comfy

        try:
            client = comfy.ComfyUI(comfy_cfg.get("url"), timeout=int(comfy_cfg.get("timeout", 1800)))
            workflow = _comfy_workflow(series, comfy_cfg, "portrait")
            for index, cid in enumerate(todo):
                values = {"prompt": portrait_prompt(series.characters[cid]), "seed": 1000 + index,
                          "width": int(comfy_cfg.get("width", 720)), "height": int(comfy_cfg.get("height", 1280)),
                          "prefix": f"{series.slug}-{cid}-portrait"}
                client.run(workflow, values, folder / f"{cid}.photo.png", comfy.IMAGE_SUFFIXES)
                logger.info(f"portrait: {folder / f'{cid}.photo.png'}")
        except comfy.ComfyError as exc:
            raise SeriesError(str(exc)) from exc
        todo = []

    def one(cid):
        path = folder / f"{cid}.photo.png"
        if cid in todo:
            real.make_portrait(series.characters[cid], path, model)
            logger.info(f"portrait: {path}")
        return cid, path

    with ThreadPoolExecutor(LLM_WORKERS) as pool:
        return dict(pool.map(one, series.characters))


def _render_realistic(series: Series, episode: dict, folder: Path, force: bool) -> Path:
    """Live-action clips with spoken dialogue from Gemini Omni. Returns the combined video."""
    from app.services import series_realistic as real

    settings = series.bible.get("render") or {}
    model = settings.get("video_model", real.DEFAULT_VIDEO_MODEL)
    resolution = settings.get("resolution", "720p")
    portraits = ensure_portraits(series)
    raw = folder / "clips" / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    def one(scene):
        out = raw / f"{scene['id']}.mp4"
        if force or not out.exists():
            real.make_clip(series.bible, scene, portraits, out, model, resolution)
        return out

    logger.info(f"generating {len(episode['scenes'])} live-action clips with {model}")
    with ThreadPoolExecutor(3) as pool:
        raws = list(pool.map(one, episode["scenes"]))

    return _finish_spoken_clips(episode, folder, raws)


def _finish_spoken_clips(episode: dict, folder: Path, raws: list[Path]) -> Path:
    """Clips that already carry their own spoken audio: normalise, caption by line length, join."""
    from app.services import series_realistic as real

    clips, timeline, cursor = [], [], 0.0
    for scene, src in zip(episode["scenes"], raws):
        clip = folder / "clips" / f"{scene['id']}.mp4"
        real.normalise_clip(_ffmpeg(), src, clip, WIDTH, HEIGHT, FPS)
        seconds = real.probe_seconds(_ffmpeg(), clip)
        lines = scene.get("lines", [])
        total = sum(len(l["text"]) for l in lines) or 1
        start, span = cursor + 0.3, max(0.5, seconds - 0.8)
        for line in lines:
            length = span * len(line["text"]) / total
            timeline.append({"text": line["text"], "start": int(start * 1000), "end": int((start + length) * 1000)})
            start += length
        cursor += seconds
        clips.append(clip)
    _write_srt(timeline, folder / "subtitles.srt")
    combined = _concat(clips, folder / "clips" / "combined.mp4")
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(combined), "-vn", "-c:a", "libmp3lame",
         "-q:a", "2", str(folder / "audio" / "voice.mp3")],
        check=True,
    )
    return combined


# --------------------------------------------------------------------------
# ComfyUI visuals: open models on your own or a rented GPU
# --------------------------------------------------------------------------


def _comfy_settings(series: Series) -> dict:
    settings = dict((series.bible.get("render") or {}).get("comfy") or {})
    if not settings.get("video_workflow"):
        raise SeriesError("render.comfy.video_workflow is not set in the bible (an API-format ComfyUI export)")
    return settings


def _comfy_path(series: Series, name: str) -> Path:
    path = Path(name).expanduser()
    return path if path.is_absolute() or path.exists() else series.root / name


def _comfy_workflow(series: Series, settings: dict, kind: str) -> dict:
    """Load render.comfy.<kind>_workflow and apply render.comfy.<kind>_inputs to it."""
    from app.services import comfyui_client as comfy

    workflow = comfy.load_workflow(_comfy_path(series, settings[f"{kind}_workflow"]))
    return comfy.set_inputs(workflow, settings.get(f"{kind}_inputs"))


COMFY_VALUES = {
    "video": ["prompt", "image", "seconds", "frames", "fps", "width", "height", "seed", "prefix"],
    "still": ["prompt", "ref1", "ref2", "ref3", "ref4", "width", "height", "seed", "prefix"],
    "portrait": ["prompt", "width", "height", "seed", "prefix"],
}


def comfy_check(series: Series) -> str:
    """Check the ComfyUI server and the bible's workflows without spending GPU time."""
    from app.services import comfyui_client as comfy

    settings = _comfy_settings(series)
    lines = []
    try:
        client = comfy.ComfyUI(settings.get("url"))
        stats = client.check()
        for device in stats.get("devices", []):
            free = device.get("vram_free")
            lines.append(f"server {client.url}: {device.get('name', '?')}"
                         + (f", {free / 2**30:.1f} GB video memory free" if free else ""))
        for kind, names in COMFY_VALUES.items():
            if not settings.get(f"{kind}_workflow"):
                continue
            workflow = _comfy_workflow(series, settings, kind)
            text = json.dumps(workflow)
            used = [n for n in names if re.search(r"\{\{\s*" + n + r"\s*\}\}", text)]
            comfy.fill_workflow(workflow, dict.fromkeys(names, 1))  # raises on unknown placeholders
            lines.append(f"\n{kind} workflow {settings[f'{kind}_workflow']}: uses {', '.join(used) or 'NO placeholders'}")
            if "prompt" not in used or (kind == "video" and "image" not in used):
                lines.append("  warning: it does not take the prompt"
                             + (" and the first frame" if kind == "video" else "") + " yet")
            lines += [f"  {line}" for line in comfy.describe_workflow(workflow)]
    except comfy.ComfyError as exc:
        raise SeriesError(str(exc)) from exc
    return "\n".join(lines)


def _setting_text(bible: dict) -> str:
    setting = bible.get("setting") or {}
    return " ".join(str(v) for v in setting.values()) if isinstance(setting, dict) else str(setting)


def comfy_shot_prompt(bible: dict, scene: dict, spoken: bool) -> str:
    """Prompt for an image-to-video model: what happens in the shot, and who says what."""
    chars = bible.get("characters", {})
    people = [f"{chars[cid]['name']} ({chars[cid].get('look', '')})" for cid in scene.get("characters", [])]
    parts = [
        "Photorealistic live-action shot from a vertical TV drama. One continuous take, natural light, "
        "cinema camera, real skin texture. No on-screen text, no subtitles, no music.",
        "" if scene.get("scene") else f"Setting: {_setting_text(bible)}",  # shot-list shots name their location
        f"People in frame: {'; '.join(people)}." if people else "",
        f"Action: {scene['shot']}",
    ]
    if spoken and scene.get("lines"):
        for line in scene["lines"]:
            c = chars[line["speaker"]]
            how = f"; {line['delivery']}" if line.get("delivery") else ""
            parts.append(f"{c['name']} speaks ({c.get('voice_style', 'natural voice')}{how}): \"{line['text']}\"")
        parts.append("Lips move in sync with the words. Quiet natural ambience of the location underneath.")
    elif scene.get("lines"):
        speakers = ", ".join(dict.fromkeys(chars[l["speaker"]]["name"] for l in scene["lines"]))
        parts.append(f"{speakers} is talking, mouth moving naturally.")
    else:
        parts.append("Nobody speaks. Natural ambience of the location.")
    return "\n".join(p for p in parts if p)


def _has_audio(path: Path) -> bool:
    info = subprocess.run([_ffmpeg(), "-i", str(path)], capture_output=True, text=True).stderr
    return "Audio:" in info


def _fit_clip(src: Path, out: Path, seconds: float | None, keep_audio: bool) -> None:
    """Scale a clip to the episode frame. With ``seconds`` it is trimmed, or its last frame held,
    to exactly that length. Spoken clips always leave with an audio track (silence if the model
    made none) so they can be joined."""
    vf = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,crop={WIDTH}:{HEIGHT},fps={FPS},format=yuv420p"
    if seconds:
        vf += f",tpad=stop_mode=clone:stop_duration={seconds:.3f}"
    cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src)]
    silent_fill = keep_audio and not _has_audio(src)
    if silent_fill:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]
    if keep_audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0" if silent_fill else "0:a:0",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k"]
        if silent_fill:
            cmd += ["-shortest"]
    else:
        cmd += ["-an"]
    if seconds:
        cmd += ["-t", f"{seconds:.3f}"]
    subprocess.run(cmd + [str(out)], check=True)


def _render_comfy(series: Series, episode: dict, folder: Path, force: bool) -> Path:
    """Full-motion clips from open models in ComfyUI.

    render.comfy.audio decides where the voices come from:
      clip   - the video model speaks the lines itself (LTX-style models with sound)
      kokoro - local Kokoro voices over silent clips (Wan / Hunyuan style models)
    """
    from app.services import comfyui_client as comfy
    from app.services import series_realistic as real

    settings = _comfy_settings(series)
    audio_mode = settings.get("audio", "clip")
    if audio_mode not in ("clip", "kokoro"):
        raise SeriesError(f"render.comfy.audio must be 'clip' or 'kokoro', not {audio_mode!r}")
    frames_from = settings.get("first_frames", "workflow" if settings.get("still_workflow") else "portrait")
    fps = int(settings.get("fps", 24))
    width, height = int(settings.get("width", 720)), int(settings.get("height", 1280))
    max_seconds = int(settings.get("max_seconds", 10))
    tries = 1 + int(settings.get("retries", 1))

    client = comfy.ComfyUI(settings.get("url"), timeout=int(settings.get("timeout", 1800)))
    try:
        stats = client.check()
        video_workflow = _comfy_workflow(series, settings, "video")
        still_workflow = _comfy_workflow(series, settings, "still") if frames_from == "workflow" else None
    except comfy.ComfyError as exc:
        raise SeriesError(str(exc)) from exc
    devices = ", ".join(d.get("name", "?") for d in stats.get("devices", [])) or "unknown device"
    logger.info(f"comfyui at {client.url}: {devices}")

    timeline = None
    if audio_mode == "kokoro":  # free and local, so a voice problem shows before any GPU time
        logger.info("recording voices")
        timeline = _voice_lines(series, episode, folder / "audio", force)
        _write_srt(timeline, folder / "subtitles.srt")

    portraits = ensure_portraits(series)
    uploaded: dict[str, str] = {}

    def upload(path: Path) -> str:
        key = str(path)
        if key not in uploaded:
            uploaded[key] = client.upload_image(path)
        return uploaded[key]

    def cast_order(scene: dict) -> list[str]:
        lead = series.bible.get("protagonist")
        speakers = [line["speaker"] for line in scene.get("lines", [])]
        in_frame = scene.get("characters", [])
        order = dict.fromkeys(([lead] if lead in in_frame else []) + speakers + in_frame)
        return [cid for cid in order if cid in portraits]

    def first_frame(scene: dict, seed: int) -> Path:
        refs = cast_order(scene)
        out = folder / "scenes" / f"{scene['id']}.frame.png"
        if not force and out.exists():
            return out
        if frames_from == "portrait":
            if not refs:
                raise SeriesError(f"shot {scene['id']} has no character with a portrait to start from; "
                                  "set render.comfy.still_workflow or first_frames: gemini")
            return portraits[refs[0]]
        if frames_from == "gemini":
            return real.make_still(series.bible, scene, portraits, out)
        values = {"prompt": real.still_prompt(series.bible, scene, refs[:4]), "width": width,
                  "height": height, "seed": seed, "prefix": f"{series.slug}-{scene['id']}-frame"}
        for i in range(4):  # ref1..ref4: cast portraits, repeated when the shot has fewer people
            values[f"ref{i + 1}"] = upload(portraits[refs[i % len(refs)]]) if refs else ""
        return client.run(still_workflow, values, out, comfy.IMAGE_SUFFIXES)

    raw = folder / "clips" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    report = []

    def one(index: int, scene: dict) -> Path:
        out = raw / f"{scene['id']}.comfy.mp4"
        if not force and out.exists():
            return out
        if audio_mode == "kokoro":
            seconds = max(2, min(max_seconds, -(-(scene["_end_ms"] - scene["_start_ms"]) // 1000)))
        elif scene.get("seconds"):  # shot-list episodes: planned length, stretched if the line needs it
            words = sum(len(l["text"].split()) for l in scene.get("lines", []))
            needed = words / 3.0 + 0.6 if words else 0  # brisk drama delivery, a little air at the end
            seconds = max(2, min(max_seconds, math.ceil(max(scene["seconds"], needed))))
        else:
            seconds = min(max_seconds, real.clip_seconds(scene, (3, max_seconds)))
        last = None
        for attempt in range(tries):
            seed = (episode.get("number", 0) * 1000 + index) * 10 + attempt
            started = time.monotonic()
            try:
                frame = first_frame(scene, seed)
                values = {
                    "prompt": comfy_shot_prompt(series.bible, scene, spoken=audio_mode == "clip"),
                    "image": upload(frame), "seconds": seconds, "frames": seconds * fps + 1, "fps": fps,
                    "width": width, "height": height, "seed": seed,
                    "prefix": f"{series.slug}-{scene['id']}",
                }
                client.run(video_workflow, values, out, comfy.VIDEO_SUFFIXES)
                report.append({"shot": scene["id"], "seconds": seconds, "attempt": attempt + 1,
                               "gpu_seconds": round(time.monotonic() - started)})
                return out
            except comfy.ComfyError as exc:
                last = exc
                logger.warning(f"shot {scene['id']} attempt {attempt + 1} failed: {exc}")
        raise SeriesError(f"shot {scene['id']} failed after {tries} tries: {last}")

    logger.info(f"generating {len(episode['scenes'])} clips in ComfyUI ({audio_mode} audio, {frames_from} first frames)")
    raws = [one(i, scene) for i, scene in enumerate(episode["scenes"])]  # one GPU: one job at a time
    if report:
        gpu = sum(r["gpu_seconds"] for r in report)
        rate = float(settings.get("gpu_usd_per_hour", 0) or 0)
        summary = {"shots": report, "gpu_seconds": gpu, "gpu_usd": round(gpu / 3600 * rate, 3) if rate else None}
        (folder / "comfy-report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.info(f"comfyui total: {gpu}s of GPU time" + (f", about ${summary['gpu_usd']}" if rate else ""))

    if audio_mode == "clip":
        fitted = []
        for scene, src in zip(episode["scenes"], raws):
            mid = raw / f"{scene['id']}.fit.mp4"
            _fit_clip(src, mid, None, keep_audio=True)
            fitted.append(mid)
        return _finish_spoken_clips(episode, folder, fitted)

    clips = []
    for scene, src in zip(episode["scenes"], raws):
        clip = folder / "clips" / f"{scene['id']}.mp4"
        _fit_clip(src, clip, (scene["_end_ms"] - scene["_start_ms"]) / 1000, keep_audio=False)
        clips.append(clip)
    return _concat(clips, folder / "clips" / "combined.mp4")


def _add_title_card(src: Path, out: Path, text: str, font_name: str, seconds: float = 1.8) -> None:
    """Burn a 'PART N/10' banner into the first seconds (PIL image + ffmpeg overlay)."""
    from PIL import Image, ImageDraw, ImageFont

    from app.utils import utils

    font_path = Path(utils.font_dir()) / font_name
    try:
        font = ImageFont.truetype(str(font_path), 64)
    except OSError:
        font = ImageFont.load_default(64)
    card = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    w, h = right - left, bottom - top
    x, y = (WIDTH - w) // 2, 230
    draw.rounded_rectangle((x - 36, y - 26, x + w + 36, y + h + 34), radius=22, fill=(0, 0, 0, 170))
    draw.text((x - left, y - top), text, font=font, fill=(255, 255, 255, 255))
    png = out.with_suffix(".title.png")
    card.save(png)
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src), "-i", str(png), "-filter_complex",
         f"[0:v][1:v]overlay=0:0:enable='lt(t,{seconds})'[v]", "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "copy", str(out)],
        check=True,
    )
    png.unlink(missing_ok=True)


def _render_stills(series: Series, episode: dict, folder: Path, force: bool) -> Path:
    """Photoreal stills (Gemini image model, cast portraits as references) + local voices."""
    from app.services import series_realistic as real

    model = (series.bible.get("render") or {}).get("image_model", real.DEFAULT_IMAGE_MODEL)
    portraits = ensure_portraits(series)

    def one(scene):
        out = folder / "scenes" / f"{scene['id']}.photo.png"
        if force or not out.exists():
            real.make_still(series.bible, scene, portraits, out, model)
        return out

    # Free local voices first, so a script or voice problem shows up before any paid image call.
    logger.info("recording voices")
    timeline = _voice_lines(series, episode, folder / "audio", force)
    _write_srt(timeline, folder / "subtitles.srt")

    logger.info(f"generating {len(episode['scenes'])} photoreal stills with {model}")
    with ThreadPoolExecutor(LLM_WORKERS) as pool:
        stills = list(pool.map(one, episode["scenes"]))

    logger.info("animating stills")
    clips = []
    for i, (scene, png) in enumerate(zip(episode["scenes"], stills)):
        clip = folder / "clips" / f"{scene['id']}.mp4"
        _still_to_clip(png, (scene["_end_ms"] - scene["_start_ms"]) / 1000, clip, move=i)
        clips.append(clip)
    return _concat(clips, folder / "clips" / "combined.mp4")


def render_episode(series: Series, number: int, force: bool = False, bgm_volume: float = 0.12,
                   visuals: str | None = None, episode_file: str | None = None,
                   only: list[str] | None = None) -> Path:
    """Render a planned episode. ``episode_file`` renders another script for this episode number
    into its own folder (ep01-<file name>), so a test never overwrites the planned episode.
    ``only`` renders just the named scenes or shots, e.g. ["A"] or ["A1", "A2"]."""
    from app.services import series_shots

    folder = series.episode_dir(number)
    ep_file = folder / "episode.json"
    if episode_file:
        ep_file = _comfy_path(series, episode_file)
        if not ep_file.exists():
            raise SeriesError(f"no episode file at {episode_file}")
        stem = ep_file.stem if ep_file.stem.startswith(f"ep{number:02d}") else f"ep{number:02d}-{ep_file.stem}"
        folder = series.out / stem
        folder.mkdir(parents=True, exist_ok=True)
    if not ep_file.exists():
        raise SeriesError(f"episode {number} is not planned yet; run `plan --episode {number}` first")
    episode = json.loads(ep_file.read_text(encoding="utf-8"))
    try:
        episode = series_shots.flatten(series.bible, series_shots.select(episode, only))
    except ValueError as exc:
        raise SeriesError(str(exc)) from exc
    if only:
        folder = folder / f"only-{'-'.join(only)}"
        folder.mkdir(parents=True, exist_ok=True)
    for sub in ("scenes", "audio", "clips"):
        (folder / sub).mkdir(exist_ok=True)

    visuals = visuals or (series.bible.get("render") or {}).get("visuals", "drawn")
    if visuals == "realistic":
        combined = _render_realistic(series, episode, folder, force)
    elif visuals == "stills":
        combined = _render_stills(series, episode, folder, force)
    elif visuals == "comfy":
        combined = _render_comfy(series, episode, folder, force)
    elif visuals == "drawn":
        combined = _render_drawn(series, episode, folder, force)
    else:
        raise SeriesError(f"unknown visuals {visuals!r}; use 'drawn', 'stills', 'realistic' or 'comfy'")

    logger.info("mixing voice, music and subtitles")
    name = folder.name if not only else folder.parent.name
    tag = (f"-{'-'.join(only)}" if only else "")
    final = folder / f"{series.slug}-{name}{tag}{ {'realistic': '-real', 'stills': '-photo', 'comfy': '-comfy'}.get(visuals, '') }.mp4"
    captions = series.bible.get("captions") or {}
    params = VideoParams(
        video_subject=episode.get("title", f"Episode {number}"),
        video_aspect="9:16",
        subtitle_enabled=True,
        bgm_type="random",
        bgm_volume=bgm_volume,
        voice_volume=1.0,
        font_name=captions.get("font", "STHeitiMedium.ttc"),
        font_size=int(captions.get("font_size", 60)),
        stroke_width=float(captions.get("stroke_width", 1.5)),
        subtitle_position="custom" if "position_percent" in captions else "bottom",
        custom_position=float(captions.get("position_percent", 70.0)),
    )
    title_card = series.bible.get("title_card")
    mixed = folder / "clips" / "mixed.mp4" if title_card else final
    video.generate_video(str(combined), str(folder / "audio" / "voice.mp3"),
                         str(folder / "subtitles.srt"), str(mixed), params)
    if title_card:
        total = episode.get("total") or (series.bible.get("format") or {}).get("parts", "?")
        _add_title_card(mixed, final, title_card.format(n=number, total=total),
                        captions.get("font", "BeVietnamPro-Bold.ttf"))
    (folder / "caption.txt").write_text(episode.get("caption", ""), encoding="utf-8")
    logger.success(f"episode {number} ready: {final}")
    return final


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.services.series", description=__doc__.splitlines()[0])
    parser.add_argument("series", help="series folder containing bible.yaml, e.g. series/coral-key")
    sub = parser.add_subparsers(dest="command", required=True)
    sheets = sub.add_parser("characters", help="draw a reference sheet per character")
    sheets.add_argument("--force", action="store_true")
    photos = sub.add_parser("portraits", help="photoreal reference portrait per character (Gemini)")
    photos.add_argument("--force", action="store_true")
    for name in ("plan", "render"):
        p = sub.add_parser(name)
        p.add_argument("--episode", type=int, required=True)
        p.add_argument("--force", action="store_true", help="redo this step even if files exist")
    sub.choices["render"].add_argument("--visuals", choices=["drawn", "stills", "realistic", "comfy"],
                                       help="override the bible's render.visuals")
    sub.choices["render"].add_argument("--file", help="render this episode JSON instead of the planned one "
                                       "(output goes to its own folder)")
    sub.choices["render"].add_argument("--only", help="comma-separated scene or shot ids to render, e.g. A or A1,A2")
    sub.add_parser("comfy-check", help="check the ComfyUI server and workflows (no GPU time used)")
    nxt = sub.add_parser("next", help="plan and render the next unplanned episode")
    nxt.add_argument("--visuals", choices=["drawn", "stills", "realistic", "comfy"])
    args = parser.parse_args(argv)

    series = Series.load(args.series)
    try:
        if args.command == "characters":
            for path in draw_character_sheets(series, force=args.force):
                print(path.with_suffix(".png"))
        elif args.command == "portraits":
            for path in ensure_portraits(series, force=args.force).values():
                print(path)
        elif args.command == "comfy-check":
            print(comfy_check(series))
        elif args.command == "plan":
            episode = plan_episode(series, args.episode, force=args.force)
            print(json.dumps(episode, indent=2, ensure_ascii=False))
        elif args.command == "render":
            only = [o for o in (args.only or "").split(",") if o.strip()] or None
            print(render_episode(series, args.episode, force=args.force, visuals=args.visuals,
                                 episode_file=args.file, only=only))
        elif args.command == "next":
            planned = series.planned_episodes()
            number = (planned[-1] + 1) if planned else 1
            plan_episode(series, number)
            print(render_episode(series, number, visuals=args.visuals))
    except Exception as exc:
        if not isinstance(exc, SeriesError) and type(exc).__name__ != "RealisticError":
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
