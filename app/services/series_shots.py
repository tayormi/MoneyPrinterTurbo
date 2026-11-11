"""Shot-list episodes: scenes that hold short shots, written the way film crews cover a scene.

An episode in this format has ``scenes`` that each hold ``shots``. The scene owns what must
stay the same across its cuts (location, time, light, who stands where); each shot owns one
framing, one action, one eyeline and at most one spoken line. See docs/series.md.

``flatten`` turns such an episode into the flat list of one-shot "scenes" the renderers use,
compiling each shot's fields into a fixed-order description, so continuity wording (frame
sides, light, eyelines) is repeated identically in every prompt of a scene.
"""

from loguru import logger


def is_shot_list(episode: dict) -> bool:
    return any("shots" in scene for scene in episode.get("scenes", []))


def select(episode: dict, only: list[str] | None) -> dict:
    """Keep only the named scenes (or shots). Works for both episode formats."""
    if not only:
        return episode
    wanted = {o.strip() for o in only if o.strip()}
    kept = []
    for scene in episode.get("scenes", []):
        if scene.get("id") in wanted:
            kept.append(scene)
        elif "shots" in scene:
            shots = [s for s in scene["shots"] if s.get("id") in wanted]
            if shots:
                kept.append({**scene, "shots": shots})
    if not kept:
        raise ValueError(f"no scene or shot called {', '.join(sorted(wanted))} in this episode")
    return {**episode, "scenes": kept}


def _location_text(bible: dict, scene: dict) -> str:
    locations = bible.get("locations") or {}
    key = scene.get("location", "")
    desc = locations.get(key, key.replace("_", " "))
    return f"Location: {desc}." if desc else ""


def compile_shot(bible: dict, scene: dict, shot: dict) -> str:
    """One shot as prose, in the order the LTX guide recommends: shot, scene, blocking, action,
    eyeline, camera move. Speech is added by the renderer from ``lines``."""
    chars = bible.get("characters", {})
    framing = ", ".join(x for x in (shot.get("size"), shot.get("angle")) if x)
    blocking = scene.get("blocking") or {}
    in_frame = shot.get("in_frame") or []
    placed = [f"{chars.get(cid, {}).get('name', cid)} {blocking[cid]}" for cid in in_frame if cid in blocking]
    eyeline = shot.get("eyeline")
    move = shot.get("move") or "static camera"
    parts = [
        f"{framing[:1].upper() + framing[1:]}." if framing else "",
        _location_text(bible, scene),
        f"Time: {scene['time']}." if scene.get("time") else "",
        f"Light: {scene['light']}." if scene.get("light") else "",
        f"Positions: {'; '.join(placed)}." if placed else "",
        shot.get("action", ""),
        f"Eyeline: {eyeline}." if eyeline else "",
        f"Camera: {move}.",
    ]
    return " ".join(p.strip() for p in parts if p and p.strip())


def flatten(bible: dict, episode: dict) -> dict:
    """Return a copy of the episode whose ``scenes`` are single shots the renderers understand."""
    if not is_shot_list(episode):
        return episode
    known = set(bible.get("characters", {}))
    flat = []
    for scene in episode["scenes"]:
        for shot in scene.get("shots", []):
            line = shot.get("line")
            lines = [line] if line else []
            for extra in shot.get("lines", []):  # tolerated, but one speaker per shot syncs best
                lines.append(extra)
            if len(lines) > 1:
                logger.warning(f"shot {shot.get('id')} has {len(lines)} lines; one per shot lip-syncs best")
            for l in lines:
                if l.get("speaker") not in known:
                    raise ValueError(f"unknown speaker {l.get('speaker')!r} in shot {shot.get('id')}")
            in_frame = [c for c in (shot.get("in_frame") or []) if c in known]
            flat.append({
                "id": shot["id"],
                "scene": scene.get("id"),
                "shot": compile_shot(bible, scene, shot),
                "characters": in_frame,
                "lines": [{"speaker": l["speaker"], "text": l["text"], "delivery": l.get("delivery", "")}
                          for l in lines],
                "seconds": float(shot.get("seconds") or 0),
            })
    return {**episode, "scenes": flat}


def last_beat(episode: dict) -> str:
    """The final shot as text, for the next episode's planner (cliffhanger payoff)."""
    if not episode.get("scenes"):
        return ""
    last = episode["scenes"][-1]
    if "shots" in last and last["shots"]:
        shot = last["shots"][-1]
        line = shot.get("line") or {}
        return f"{shot.get('action', '')} " + (f"{line.get('speaker')}: {line.get('text')}" if line else "")
    return f"{last.get('shot', '')} " + " ".join(f"{l['speaker']}: {l['text']}" for l in last.get("lines", []))
