"""Drive a ComfyUI server from a script: upload inputs, queue a workflow, fetch the result.

ComfyUI runs open video and image models (LTX, Wan, HunyuanVideo, ...) on your own
or a rented GPU, so a clip costs only GPU time. This client talks to its HTTP API:

    POST /upload/image         send a portrait or first frame
    POST /prompt               queue a workflow (API-format JSON)
    GET  /history/{prompt_id}  poll until the job is done, read what it produced
    GET  /view                 download the produced file

Workflows are plain files exported from the ComfyUI app with "Export (API)". To make
one reusable, put placeholders in its text fields before saving it, for example
``{{prompt}}`` in the prompt box and ``{{image}}`` in the Load Image node. A field
that is exactly one placeholder takes the value with its type (so ``"{{seconds}}"``
becomes the number 8); placeholders inside longer text are filled in as text.

The server address comes from ``COMFYUI_URL`` or the ``url`` setting. ComfyUI has no
login of its own: keep it on localhost, an SSH tunnel or the GPU host's private proxy.
"""

import copy
import json
import os
import re
import time
import uuid
from pathlib import Path

import requests
from loguru import logger

VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class ComfyError(RuntimeError):
    pass


def fill_workflow(workflow: dict, values: dict) -> dict:
    """Return a copy of an API-format workflow with ``{{name}}`` placeholders filled in."""
    missing = set()

    def fill(node):
        if isinstance(node, dict):
            return {key: fill(value) for key, value in node.items()}
        if isinstance(node, list):
            return [fill(value) for value in node]
        if not isinstance(node, str):
            return node
        whole = _PLACEHOLDER.fullmatch(node.strip())
        if whole:
            if whole.group(1) not in values:
                missing.add(whole.group(1))
                return node
            return values[whole.group(1)]

        def text(match):
            if match.group(1) not in values:
                missing.add(match.group(1))
                return match.group(0)
            return str(values[match.group(1)])

        return _PLACEHOLDER.sub(text, node)

    filled = fill(copy.deepcopy(workflow))
    if missing:
        raise ComfyError(f"workflow uses placeholders with no value: {', '.join(sorted(missing))}")
    return filled


def set_inputs(workflow: dict, mapping: dict | None) -> dict:
    """Return a copy of a workflow with some node inputs replaced.

    ``mapping`` is ``{"<node id>.<input name>": value}``, for example
    ``{"6.text": "{{prompt}}", "27.length": "{{frames}}"}``. It puts placeholders into a
    plain export without editing the file, including number fields that the ComfyUI
    app will not let you type text into.
    """
    workflow = copy.deepcopy(workflow)
    for target, value in (mapping or {}).items():
        node_id, _, name = str(target).partition(".")
        node = workflow.get(node_id)
        if not isinstance(node, dict) or not name:
            raise ComfyError(f"workflow has no node {node_id!r} (from input setting {target!r})")
        if name not in node.get("inputs", {}):
            known = ", ".join(sorted(node.get("inputs", {}))) or "none"
            raise ComfyError(f"node {node_id} ({node.get('class_type')}) has no input {name!r}; it has: {known}")
        node["inputs"][name] = value
    return workflow


def describe_workflow(workflow: dict) -> list[str]:
    """One line per input you can set (links between nodes are left out)."""
    lines = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        title = (node.get("_meta") or {}).get("title") or node["class_type"]
        for name, value in node.get("inputs", {}).items():
            if isinstance(value, list):
                continue
            shown = json.dumps(value, ensure_ascii=False)
            lines.append(f"{node_id}.{name} = {shown if len(shown) <= 70 else shown[:67] + '...'}   [{title}]")
    return lines


def load_workflow(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise ComfyError(f"workflow file not found: {path}")
    workflow = json.loads(path.read_text(encoding="utf-8"))
    if "nodes" in workflow and "links" in workflow:
        raise ComfyError(
            f"{path} is a normal ComfyUI save, not an API export. "
            "In ComfyUI use Workflow > Export (API) and save that file instead."
        )
    return workflow


class ComfyUI:
    def __init__(self, url: str | None = None, timeout: int = 1800, poll_seconds: float = 2.0):
        self.url = (url or os.environ.get("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.session = requests.Session()
        token = os.environ.get("COMFYUI_API_KEY", "").strip()
        if token:  # only for servers that sit behind a token-checking proxy
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _call(self, method: str, path: str, **kwargs):
        try:
            resp = self.session.request(method, f"{self.url}{path}", timeout=kwargs.pop("timeout", 120), **kwargs)
        except requests.RequestException as exc:
            raise ComfyError(f"cannot reach ComfyUI at {self.url}: {exc}") from exc
        return resp

    def check(self) -> dict:
        """Server details, or a clear error if it is not reachable."""
        resp = self._call("GET", "/system_stats", timeout=20)
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI at {self.url} answered HTTP {resp.status_code}")
        return resp.json()

    def upload_image(self, path: str | Path) -> str:
        """Send an image to the server's input folder. Returns the name to use in Load Image."""
        path = Path(path)
        with path.open("rb") as handle:
            resp = self._call("POST", "/upload/image",
                              files={"image": (path.name, handle, "application/octet-stream")},
                              data={"overwrite": "true"})
        if resp.status_code != 200:
            raise ComfyError(f"upload of {path.name} failed: HTTP {resp.status_code} {resp.text[:300]}")
        info = resp.json()
        return f"{info['subfolder']}/{info['name']}" if info.get("subfolder") else info["name"]

    def queue(self, workflow: dict) -> str:
        resp = self._call("POST", "/prompt", json={"prompt": workflow, "client_id": uuid.uuid4().hex})
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI rejected the workflow: HTTP {resp.status_code} {resp.text[:800]}")
        data = resp.json()
        if data.get("node_errors"):
            raise ComfyError(f"workflow has node errors: {json.dumps(data['node_errors'])[:800]}")
        return data["prompt_id"]

    def wait(self, prompt_id: str) -> dict:
        """Block until the job finishes. Returns its history entry."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            resp = self._call("GET", f"/history/{prompt_id}")
            entry = resp.json().get(prompt_id) if resp.status_code == 200 else None
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    detail = [m[1].get("exception_message", "") for m in status.get("messages", [])
                              if m and m[0] == "execution_error"]
                    raise ComfyError(f"ComfyUI job failed: {' '.join(detail).strip() or 'see the server log'}")
                if entry.get("outputs") or status.get("completed"):
                    return entry
            time.sleep(self.poll_seconds)
        # Do not queue again from here: the job may still be running on a paid GPU.
        self._call("POST", "/interrupt", timeout=20)
        raise ComfyError(f"ComfyUI job {prompt_id} did not finish in {self.timeout}s; it was interrupted")

    @staticmethod
    def output_files(entry: dict, suffixes: tuple[str, ...]) -> list[dict]:
        found = []
        for node_output in (entry.get("outputs") or {}).values():
            for items in node_output.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(suffixes):
                        found.append(item)
        return found

    def download(self, item: dict, out: str | Path) -> Path:
        out = Path(out)
        resp = self._call("GET", "/view", timeout=600, params={
            "filename": item["filename"], "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output")})
        if resp.status_code != 200 or not resp.content:
            raise ComfyError(f"download of {item['filename']} failed: HTTP {resp.status_code}")
        partial = out.with_name(out.name + ".part")  # never leave a half-written file that looks done
        partial.write_bytes(resp.content)
        partial.replace(out)
        return out

    def run(self, workflow: dict, values: dict, out: str | Path, suffixes: tuple[str, ...]) -> Path:
        """Fill a workflow, run it and save its first matching output file to ``out``."""
        started = time.monotonic()
        entry = self.wait(self.queue(fill_workflow(workflow, values)))
        files = self.output_files(entry, suffixes)
        if not files:
            raise ComfyError(f"the workflow finished but saved no {'/'.join(suffixes)} file; "
                             "check that it ends in a save node")
        path = self.download(files[-1], out)
        logger.info(f"comfyui: {path.name} in {time.monotonic() - started:.0f}s")
        return path
