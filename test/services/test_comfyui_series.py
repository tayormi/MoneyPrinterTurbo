"""ComfyUI client and the series `comfy` visuals, against a stand-in ComfyUI server.

The stand-in copies the request and reply shapes of ComfyUI's own server.py
(/system_stats, /upload/image, /prompt, /history/{id}, /view) and "renders" a
test-pattern clip with ffmpeg, so no GPU or model is needed.
"""

import email
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import comfyui_client as comfy
from app.services import series as series_mod


def _ffmpeg(*args):
    subprocess.run([series_mod._ffmpeg(), "-y", "-loglevel", "error", *args], check=True)


class _FakeComfy(BaseHTTPRequestHandler):
    state = None  # set per server: {"dir", "uploads", "prompts", "history", "fail_first", "audio"}

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        st = self.state
        if url.path == "/system_stats":
            return self._json({"devices": [{"name": "fake RTX 4090"}]})
        if url.path.startswith("/history/"):
            pid = url.path.rsplit("/", 1)[1]
            return self._json({pid: st["history"][pid]} if pid in st["history"] else {})
        if url.path == "/view":
            query = parse_qs(url.query)
            data = (Path(st["dir"]) / query["filename"][0]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        st = self.state
        if self.path == "/upload/image":
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            form = email.message_from_bytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
            parts = {part.get_param("name", header="content-disposition"): part for part in form.get_payload()}
            name = parts["image"].get_filename()
            st["uploads"].append((name, parts["overwrite"].get_payload().strip()))
            return self._json({"name": name, "subfolder": "", "type": "input"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        if self.path == "/interrupt":
            return self._json({})
        if self.path == "/prompt":
            workflow = body["prompt"]
            if "{{" in json.dumps(workflow):
                return self._json({"error": {"type": "unfilled"}, "node_errors": {}}, 400)
            st["prompts"].append(workflow)
            pid = str(uuid.uuid4())
            if st["fail_first"]:
                st["fail_first"] -= 1
                st["history"][pid] = {"outputs": {}, "status": {
                    "status_str": "error", "completed": False,
                    "messages": [["execution_error", {"exception_message": "CUDA out of memory"}]]}}
                return self._json({"prompt_id": pid, "number": 1, "node_errors": {}})
            save = next(n for n in workflow.values() if n["class_type"].startswith("Save"))
            if save["class_type"] == "SaveImage":
                name = f"{save['inputs']['filename_prefix']}_00001_.png"
                _ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=360x640", "-frames:v", "1", str(Path(st["dir"]) / name))
                key = "images"
            else:
                seconds = workflow["2"]["inputs"]["seconds"]
                name = f"{save['inputs']['filename_prefix']}_00001_.mp4"
                args = ["-f", "lavfi", "-i", f"testsrc=s=360x640:r=24:d={seconds}"]
                if st["audio"]:
                    args += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:a", "aac", "-shortest"]
                _ffmpeg(*args, "-pix_fmt", "yuv420p", str(Path(st["dir"]) / name))
                key = "images"  # video save nodes report under mixed keys; the client matches by file type
            st["history"][pid] = {"outputs": {"9": {key: [{"filename": name, "subfolder": "", "type": "output"}]}},
                                  "status": {"status_str": "success", "completed": True, "messages": []}}
            return self._json({"prompt_id": pid, "number": 1, "node_errors": {}})
        self._json({"error": "not found"}, 404)


VIDEO_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
    "2": {"class_type": "FakeVideoModel", "inputs": {
        "text": "{{prompt}}", "seconds": "{{seconds}}", "frames": "{{frames}}", "seed": "{{seed}}",
        "size": "{{width}}x{{height}}", "start": ["1", 0]}},
    "9": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "{{prefix}}", "video": ["2", 0]}},
}
STILL_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "{{ref1}}"}},
    "2": {"class_type": "FakeImageModel", "inputs": {"text": "{{prompt}}", "seed": "{{seed}}", "ref": ["1", 0]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "{{prefix}}", "images": ["2", 0]}},
}
BIBLE = """
title: Test Key
slug: test-key
protagonist: leo
setting:
  place: "A small island beach."
format: {parts: 2, shot_seconds: [3, 6]}
characters:
  leo: {name: Leo, role: castaway, look: "tall man, torn white shirt", voice: "kokoro:am_adam", voice_style: calm}
  ana: {name: Ana, role: captain, look: "woman, red jacket", voice: "kokoro:af_bella", voice_style: sharp}
render:
  visuals: comfy
  comfy:
    video_workflow: video.json
    still_workflow: still.json
    audio: %(audio)s
    retries: 1
    gpu_usd_per_hour: 0.34
"""
EPISODE = {"number": 1, "total": 2, "title": "The Beach", "caption": "Part 1",
           "scenes": [
               {"id": "s01", "shot": "Leo walks up the beach.", "characters": ["leo"],
                "lines": [{"speaker": "leo", "text": "Somebody has to find water today."}]},
               {"id": "s02", "shot": "Ana blocks his way.", "characters": ["ana", "leo"],
                "lines": [{"speaker": "ana", "text": "Not you."}, {"speaker": "leo", "text": "Then who?"}]},
           ]}


class ComfySeriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = {"dir": str(self.root / "server"), "uploads": [], "prompts": [], "history": {},
                      "fail_first": 0, "audio": True}
        (self.root / "server").mkdir()
        handler = type("Handler", (_FakeComfy,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        out = self.root / "out"  # keep generated files out of the repo's storage folder
        out.mkdir()
        self.out_patch = patch.object(series_mod.Series, "out", property(lambda series: out))
        self.out_patch.start()

    def tearDown(self):
        self.out_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _series(self, audio="clip"):
        folder = self.root / "series"
        folder.mkdir(exist_ok=True)
        (folder / "bible.yaml").write_text(BIBLE % {"audio": audio}, encoding="utf-8")
        (folder / "video.json").write_text(json.dumps(VIDEO_WORKFLOW), encoding="utf-8")
        (folder / "still.json").write_text(json.dumps(STILL_WORKFLOW), encoding="utf-8")
        series = series_mod.Series.load(str(folder))
        chars = series.out / "characters"
        chars.mkdir(parents=True, exist_ok=True)
        for cid in ("leo", "ana"):  # portraits already made: no paid image call in the test
            _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=360x640", "-frames:v", "1", str(chars / f"{cid}.photo.png"))
        ep = series.episode_dir(1)
        for sub in ("scenes", "audio", "clips"):
            (ep / sub).mkdir(parents=True, exist_ok=True)
        return series, ep

    def test_fill_workflow_keeps_types_and_reports_missing(self):
        filled = comfy.fill_workflow(VIDEO_WORKFLOW, {"image": "a.png", "prompt": "hi", "seconds": 8, "frames": 193,
                                                      "seed": 5, "width": 720, "height": 1280, "prefix": "x"})
        self.assertEqual(filled["2"]["inputs"]["seconds"], 8)
        self.assertEqual(filled["2"]["inputs"]["size"], "720x1280")
        self.assertEqual(filled["2"]["inputs"]["start"], ["1", 0])
        self.assertEqual(VIDEO_WORKFLOW["2"]["inputs"]["seconds"], "{{seconds}}")
        with self.assertRaisesRegex(comfy.ComfyError, "prompt"):
            comfy.fill_workflow(VIDEO_WORKFLOW, {"image": "a.png"})

    def test_ui_format_workflow_is_refused(self):
        path = self.root / "ui.json"
        path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        with self.assertRaisesRegex(comfy.ComfyError, r"Export \(API\)"):
            comfy.load_workflow(path)

    def test_unreachable_server_is_a_clear_error(self):
        with self.assertRaisesRegex(comfy.ComfyError, "cannot reach ComfyUI"):
            comfy.ComfyUI("http://127.0.0.1:9").check()

    def test_spoken_clips_end_to_end(self):
        series, ep = self._series("clip")
        with patch.dict("os.environ", {"COMFYUI_URL": self.url}):
            combined = series_mod._render_comfy(series, json.loads(json.dumps(EPISODE)), ep, force=False)
        self.assertTrue(combined.exists())
        self.assertTrue((ep / "audio" / "voice.mp3").exists())
        self.assertIn("Somebody has to find water", (ep / "subtitles.srt").read_text())
        # two first frames + two clips queued, portraits uploaded once each, frames uploaded
        kinds = [next(n["class_type"] for n in w.values() if n["class_type"].startswith("Save")) for w in self.state["prompts"]]
        self.assertEqual(kinds, ["SaveImage", "SaveVideo", "SaveImage", "SaveVideo"])
        self.assertTrue(all(overwrite == "true" for _, overwrite in self.state["uploads"]))
        video_prompt = self.state["prompts"][1]["2"]["inputs"]
        self.assertIn('Leo says in a calm voice: "Somebody has to find water today."', video_prompt["text"])
        self.assertIsInstance(video_prompt["seconds"], int)
        report = json.loads((ep / "comfy-report.json").read_text())
        self.assertEqual(len(report["shots"]), 2)
        self.assertIsNotNone(report["gpu_usd"])
        # a second run reuses the finished clips: nothing new is queued
        queued = len(self.state["prompts"])
        with patch.dict("os.environ", {"COMFYUI_URL": self.url}):
            series_mod._render_comfy(series, json.loads(json.dumps(EPISODE)), ep, force=False)
        self.assertEqual(len(self.state["prompts"]), queued)

    def test_inputs_set_from_the_bible_on_a_plain_export(self):
        plain = json.loads(json.dumps(VIDEO_WORKFLOW))
        plain["2"]["inputs"].update({"seconds": 5, "frames": 121, "seed": 42, "text": "a cat"})
        mapped = comfy.set_inputs(plain, {"2.seconds": "{{seconds}}", "2.text": "{{prompt}}"})
        self.assertEqual(mapped["2"]["inputs"]["seconds"], "{{seconds}}")
        self.assertEqual(plain["2"]["inputs"]["seconds"], 5)
        with self.assertRaisesRegex(comfy.ComfyError, "no input 'length'.*seconds"):
            comfy.set_inputs(plain, {"2.length": "{{frames}}"})
        with self.assertRaisesRegex(comfy.ComfyError, "no node '77'"):
            comfy.set_inputs(plain, {"77.text": "x"})

        series, ep = self._series("clip")
        (series.root / "video.json").write_text(json.dumps(plain), encoding="utf-8")
        series.bible["render"]["comfy"]["video_inputs"] = {"2.seconds": "{{seconds}}", "2.text": "{{prompt}}"}
        with patch.dict("os.environ", {"COMFYUI_URL": self.url}):
            report = series_mod.comfy_check(series)
            self.assertIn("fake RTX 4090", report)
            self.assertIn("2.seed = 42", report)
            series_mod._render_comfy(series, json.loads(json.dumps(EPISODE)), ep, force=False)
        self.assertIn("Leo says", self.state["prompts"][1]["2"]["inputs"]["text"])
        self.assertEqual(self.state["prompts"][1]["2"]["inputs"]["seed"], 42)

    def test_silent_model_output_still_joins(self):
        self.state["audio"] = False
        series, ep = self._series("clip")
        with patch.dict("os.environ", {"COMFYUI_URL": self.url}):
            combined = series_mod._render_comfy(series, json.loads(json.dumps(EPISODE)), ep, force=False)
        self.assertTrue(series_mod._has_audio(combined))

    def test_failed_job_is_retried_once_then_reported(self):
        series, ep = self._series("clip")
        self.state["fail_first"] = 1
        with patch.dict("os.environ", {"COMFYUI_URL": self.url}):
            series_mod._render_comfy(series, json.loads(json.dumps(EPISODE)), ep, force=False)
        self.assertEqual(json.loads((ep / "comfy-report.json").read_text())["shots"][0]["attempt"], 2)

        series, ep = self._series("clip")
        for clip in (ep / "clips" / "raw").glob("*"):
            clip.unlink()
        for frame in (ep / "scenes").glob("*"):
            frame.unlink()
        self.state["fail_first"] = 5
        with patch.dict("os.environ", {"COMFYUI_URL": self.url}):
            with self.assertRaisesRegex(series_mod.SeriesError, "CUDA out of memory"):
                series_mod._render_comfy(series, json.loads(json.dumps(EPISODE)), ep, force=False)

    def test_kokoro_voices_over_silent_clips(self):
        series, ep = self._series("kokoro")

        def fake_voices(series_, episode, folder, force):
            cursor = 200
            for scene in episode["scenes"]:
                scene["_start_ms"], cursor = cursor, cursor + 2600
                scene["_end_ms"] = cursor
            _ffmpeg("-f", "lavfi", "-i", "sine=f=300:d=5.4", str(folder / "voice.mp3"))
            return [{"text": "x", "start": 200, "end": 2000}]

        with patch.dict("os.environ", {"COMFYUI_URL": self.url}), patch.object(series_mod, "_voice_lines", fake_voices):
            combined = series_mod._render_comfy(series, json.loads(json.dumps(EPISODE)), ep, force=False)
        from app.services import series_realistic as real

        self.assertFalse(series_mod._has_audio(combined))
        self.assertAlmostEqual(real.probe_seconds(series_mod._ffmpeg(), combined), 5.2, delta=0.15)
        self.assertIn("mouth moving", self.state["prompts"][1]["2"]["inputs"]["text"])


if __name__ == "__main__":
    unittest.main()
