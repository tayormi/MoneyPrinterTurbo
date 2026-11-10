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
import re
import subprocess
import sys
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

    recaps = []
    for previous in range(1, number):
        prev_file = series.out / f"ep{previous:02d}" / "episode.json"
        if prev_file.exists():
            prev = json.loads(prev_file.read_text(encoding="utf-8"))
            recaps.append(f"Ep {previous} \"{prev.get('title')}\": {prev.get('recap')}")
    arc = series.bible.get("season_arc") or []
    arc_beat = arc[number - 1] if 0 < number <= len(arc) else "Continue the story naturally from the recaps."
    fmt = series.bible.get("format", {})
    char_ids = ", ".join(series.characters)

    prompt = f"""You are the head writer of a vertical short-video series. Write episode {number}.

SERIES BIBLE (follow it, including the guardrails):
{series.bible_text()}

STORY SO FAR:
{chr(10).join(recaps) if recaps else "This is the first episode."}

THIS EPISODE'S BEAT: {arc_beat}

Write {fmt.get('scenes_per_episode', [7, 9])} scenes and {fmt.get('spoken_words_per_episode', [170, 230])} spoken words in total.
Each scene is one illustrated shot with 1-4 spoken lines. Lines are short and natural for speech.
Use only these speaker ids: {char_ids}.

Return ONLY JSON in this exact shape:
{{
  "title": "short episode title",
  "caption": "TikTok caption, one punchy sentence plus 3-5 hashtags",
  "scenes": [
    {{
      "id": "s1",
      "shot": "one-paragraph visual description of the single illustrated frame: location, time of day, who is in frame, their poses and expressions, framing (wide / medium / close-up)",
      "characters": ["leo", "vanessa"],
      "lines": [{{"speaker": "leo", "text": "..."}}]
    }}
  ],
  "recap": "3-4 sentence summary of what happened, for writing the next episode"
}}"""
    episode = _extract_json(_ask(prompt))
    known = set(series.characters)
    for scene in episode.get("scenes", []):
        scene["characters"] = [c for c in scene.get("characters", []) if c in known]
        for line in scene.get("lines", []):
            if line.get("speaker") not in known:
                raise SeriesError(f"unknown speaker {line.get('speaker')!r} in {scene.get('id')}")
    episode["number"] = number
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


def _still_to_clip(png: Path, seconds: float, out: Path, zoom_in: bool) -> None:
    frames = max(1, round(seconds * FPS))
    zoom = f"1+0.08*on/{frames}" if zoom_in else f"1.08-0.08*on/{frames}"
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(png), "-vf",
         f"scale={WIDTH * 2}:{HEIGHT * 2},zoompan=z='{zoom}':d={frames}:"
         f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps={FPS},format=yuv420p",
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


def ensure_portraits(series: Series, force: bool = False) -> dict:
    """Photoreal reference portrait per character (Gemini image model)."""
    from app.services import series_realistic as real

    folder = series.out / "characters"
    folder.mkdir(parents=True, exist_ok=True)
    model = (series.bible.get("render") or {}).get("image_model", real.DEFAULT_IMAGE_MODEL)

    def one(item):
        cid, char = item
        path = folder / f"{cid}.photo.png"
        if force or not path.exists():
            real.make_portrait(char, path, model)
            logger.info(f"portrait: {path}")
        return cid, path

    with ThreadPoolExecutor(LLM_WORKERS) as pool:
        return dict(pool.map(one, series.characters.items()))


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


def render_episode(series: Series, number: int, force: bool = False, bgm_volume: float = 0.12,
                   visuals: str | None = None) -> Path:
    folder = series.episode_dir(number)
    ep_file = folder / "episode.json"
    if not ep_file.exists():
        raise SeriesError(f"episode {number} is not planned yet; run `plan --episode {number}` first")
    episode = json.loads(ep_file.read_text(encoding="utf-8"))
    for sub in ("scenes", "audio", "clips"):
        (folder / sub).mkdir(exist_ok=True)

    visuals = visuals or (series.bible.get("render") or {}).get("visuals", "drawn")
    if visuals == "realistic":
        combined = _render_realistic(series, episode, folder, force)
    elif visuals == "drawn":
        combined = _render_drawn(series, episode, folder, force)
    else:
        raise SeriesError(f"unknown visuals {visuals!r}; use 'drawn' or 'realistic'")

    logger.info("mixing voice, music and subtitles")
    final = folder / f"{series.slug}-ep{number:02d}{'-real' if visuals == 'realistic' else ''}.mp4"
    params = VideoParams(
        video_subject=episode.get("title", f"Episode {number}"),
        video_aspect="9:16",
        subtitle_enabled=True,
        bgm_type="random",
        bgm_volume=bgm_volume,
        voice_volume=1.0,
    )
    video.generate_video(str(combined), str(folder / "audio" / "voice.mp3"),
                         str(folder / "subtitles.srt"), str(final), params)
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
    sub.choices["render"].add_argument("--visuals", choices=["drawn", "realistic"],
                                       help="override the bible's render.visuals")
    nxt = sub.add_parser("next", help="plan and render the next unplanned episode")
    nxt.add_argument("--visuals", choices=["drawn", "realistic"])
    args = parser.parse_args(argv)

    series = Series.load(args.series)
    try:
        if args.command == "characters":
            for path in draw_character_sheets(series, force=args.force):
                print(path.with_suffix(".png"))
        elif args.command == "portraits":
            for path in ensure_portraits(series, force=args.force).values():
                print(path)
        elif args.command == "plan":
            episode = plan_episode(series, args.episode, force=args.force)
            print(json.dumps(episode, indent=2, ensure_ascii=False))
        elif args.command == "render":
            print(render_episode(series, args.episode, force=args.force, visuals=args.visuals))
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
