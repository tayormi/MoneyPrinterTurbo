import json

import pytest

from app.services import series


def test_extract_json_strips_fences():
    reply = '```json\n{"title": "T", "scenes": []}\n```'
    assert series._extract_json(reply) == {"title": "T", "scenes": []}


def test_extract_svg_adds_namespace():
    svg = series._extract_svg('Here you go: <svg viewBox="0 0 10 10"><rect/></svg> done')
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert svg.endswith("</svg>")


def test_extract_svg_rejects_missing_markup():
    with pytest.raises(series.SeriesError):
        series._extract_svg("no drawing here")


def test_srt_splits_long_lines_and_keeps_timing(tmp_path):
    timeline = [
        {"text": "This tarp is for the passengers. Staff can sleep elsewhere.", "start": 200, "end": 3700},
        {"text": "Try the rocks.", "start": 4000, "end": 5000},
    ]
    path = tmp_path / "subs.srt"
    series._write_srt(timeline, path)
    cues = path.read_text(encoding="utf-8").strip().split("\n\n")
    assert len(cues) == 3
    assert cues[0].splitlines()[1].startswith("00:00:00,200 --> ")
    assert cues[1].splitlines()[1].endswith("--> 00:00:03,700")
    assert all(len(c.splitlines()[2]) <= series.MAX_SUBTITLE_CHARS for c in cues)
    assert cues[2].splitlines()[1] == "00:00:04,000 --> 00:00:05,000"


def test_plan_rejects_unknown_speaker(tmp_path, monkeypatch):
    (tmp_path / "bible.yaml").write_text(
        "title: T\ncharacters:\n  leo: {name: Leo, voice: 'kokoro:am_michael'}\n", encoding="utf-8"
    )
    s = series.Series.load(str(tmp_path))
    monkeypatch.setattr(series.Series, "out", property(lambda self: tmp_path / "out"))
    (tmp_path / "out").mkdir()
    plan = {"title": "x", "scenes": [{"id": "s1", "shot": "beach", "characters": ["leo"],
                                       "lines": [{"speaker": "ghost", "text": "boo"}]}], "recap": "r"}
    monkeypatch.setattr(series, "_ask", lambda prompt: json.dumps(plan))
    with pytest.raises(series.SeriesError):
        series.plan_episode(s, 1)


def test_coral_key_bible_is_complete():
    s = series.Series.load("series/coral-key")
    assert s.characters
    for char in s.characters.values():
        assert char["voice"] and char["look"] and char["name"]
    assert len(s.bible["season_arc"]) == 10


def test_realistic_refs_prefer_protagonist(tmp_path, monkeypatch):
    from app.services import series_realistic as real

    bible = {"protagonist": "leo", "setting": {}, "characters": {
        "leo": {"name": "Leo", "look": "blue shirt"},
        "vanessa": {"name": "Vanessa", "look": "white blazer", "voice_style": "cool"},
        "jade": {"name": "Jade", "look": "green top"}}}
    scene = {"id": "s1", "shot": "beach", "characters": ["vanessa", "jade", "leo"],
             "lines": [{"speaker": "vanessa", "text": "Staff sleep elsewhere."},
                       {"speaker": "jade", "text": "Try the rocks."}]}
    for cid in bible["characters"]:
        (tmp_path / f"{cid}.png").write_bytes(b"png")
    sent = {}
    monkeypatch.setattr(real, "_post", lambda path, body, timeout: sent.update(body) or
                        {"steps": [{"content": [{"type": "video", "data": "AAAA"}]}]})
    real.make_clip(bible, scene, {c: tmp_path / f"{c}.png" for c in bible["characters"]}, tmp_path / "out.mp4")
    prompt = sent["input"][0]["text"]
    assert "Leo is the person in <IMAGE_REF_0>" in prompt
    assert "Vanessa is the person in <IMAGE_REF_1>" in prompt
    assert "Jade: green top" in prompt
    assert len(sent["input"]) == 3  # text + 2 reference images
    assert sent["response_format"]["aspect_ratio"] == "9:16"


def test_clip_seconds_bounds():
    from app.services.series_realistic import clip_seconds

    assert clip_seconds({"lines": []}) == 5
    assert clip_seconds({"lines": [{"text": "word " * 200}]}) == 15


def test_photoreal_still_uses_portraits_of_characters_in_frame(tmp_path, monkeypatch):
    from app.services import series_realistic as real

    bible = {"protagonist": "leo", "setting": {}, "characters": {
        "leo": {"name": "Leo", "look": "blue shirt"},
        "mia": {"name": "Mia", "look": "yellow hoodie"},
        "jade": {"name": "Jade", "look": "green top"}}}
    scene = {"id": "s3", "shot": "close-up", "characters": ["mia", "leo"],
             "lines": [{"speaker": "jade", "text": "Off screen!"}, {"speaker": "mia", "text": "Here."}]}
    for cid in bible["characters"]:
        (tmp_path / f"{cid}.png").write_bytes(b"png")
    sent = {}
    monkeypatch.setattr(real, "_post", lambda path, body, timeout: sent.update(body) or
                        {"candidates": [{"content": {"parts": [{"inlineData": {"data": "AAAA"}}]}}]})
    real.make_still(bible, scene, {c: tmp_path / f"{c}.png" for c in bible["characters"]}, tmp_path / "s3.png")
    parts = sent["contents"][0]["parts"]
    assert "Leo is the person in reference image 1" in parts[0]["text"]
    assert "Mia is the person in reference image 2" in parts[0]["text"]
    assert len(parts) == 3  # Jade speaks off screen, so no portrait for her
    assert sent["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16"
