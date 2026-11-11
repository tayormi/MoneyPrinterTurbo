# ComfyUI mode: full-motion episodes from open models on a rented GPU

`--visuals comfy` makes every shot as a real video clip with an open model
(LTX-2.5, Wan 2.2, HunyuanVideo, ...) running in [ComfyUI](https://docs.comfy.org).
You pay only for GPU time: an RTX 4090 on RunPod was $0.34 per hour in Nov 2026,
so an episode should cost cents, not dollars. The quality of these models next to
the paid ones is the open question, and the test below is how to answer it for
about a dollar.

The fork talks to ComfyUI over its HTTP API (`app/services/comfyui_client.py`).
It uploads the first frame of each shot, queues your workflow with the shot's
prompt, waits, downloads the clip, then joins, subtitles and scores the episode
as the other modes do.

## The first test, step by step

### 1. Accounts (once)

1. **RunPod** (runpod.io): sign up and add a little credit.
2. **Hugging Face** (huggingface.co): sign up, open
   <https://huggingface.co/Lightricks/LTX-2.5> and accept the licence. The model is
   gated, so downloads fail until access shows as granted. Then create a **read**
   token under Settings > Access Tokens.
3. Optional: let Claude Code on your Mac drive RunPod for you
   ([RunPod's guide](https://agents.runpod.io/)):

   ```bash
   claude plugin marketplace add runpod/runpod-plugins-official
   claude plugin install runpod@runpod
   ```

   Then, inside Claude Code, run `/reload-plugins`, open `/mcp`, pick **runpod**
   and sign in. No API key is made or stored. After that you can say
   "follow docs/comfyui.md and set up the test pod" and approve each step.

### 2. Start a pod

- Template: **ComfyUI** (RunPod's own; "ComfyUI Blackwell Edition" for an RTX 5090).
- GPU: **RTX 4090** (24 GB). LTX-2.5's int8 files fit at moderate sizes. If it
  runs out of memory, a 48 GB card (L40S, RTX A6000) costs more per hour but is safer.
- Storage: a **network volume of 80 GB** mounted at `/workspace`, so the 39 GB of
  models survive when the pod is stopped ($0.07 per GB per month, about $5.60;
  delete the volume when you are done testing).
- The pod can take a while to boot. When it is ready, **Connect > HTTP Service
  [Port 8188]** opens ComfyUI at `https://<POD_ID>-8188.proxy.runpod.net`.

That address has no password. Anyone who has it can use your pod, so do not
share it, and stop the pod when you finish.

### 3. Download LTX-2.5

In the pod's web terminal:

```bash
pip install -U huggingface_hub
export HF_TOKEN=hf_...            # your read token; do not save it in a file
cd /workspace/madapps/ComfyUI/models
for f in \
  diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors \
  text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors \
  vae/ltx-2.5-video-vae-bf16.safetensors \
  vae/ltx-2.5-audio-vae-bf16.safetensors \
  latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
do hf download Lightricks/LTX-2.5 "$f" --local-dir . ; done
```

File names are from ComfyUI's [LTX-2.5 tutorial](https://docs.comfy.org/tutorials/video/ltx/ltx-2-5)
(Nov 2026). If the template in the next step still reports a missing model, use
the download button it shows.

### 4. Try it by hand, then export the workflow

1. In ComfyUI open **Templates > Video > LTX-2.5 I2V**. If it says nodes are
   missing, update ComfyUI (Manager > Update ComfyUI) and restart the pod.
2. Load one cast portrait, type a line of dialogue into the prompt
   (`Leo says in a calm voice: "Somebody has to find water today."`), set a
   vertical size such as 720x1280 and run it. Note how long it takes and how it looks.
3. **Workflow > Export (API)** and save the file as
   `series/coral-key/comfy/ltx25-i2v.json`. A normal save will not work; the fork
   tells you if you picked the wrong one.

### 5. Point the fork at it

Add to `series/coral-key/bible.yaml` under `render:` (there is a commented
example in that file):

```yaml
  comfy:
    video_workflow: comfy/ltx25-i2v.json
    audio: clip                 # the model speaks the lines itself
    first_frames: portrait      # start each shot from the lead character's portrait
    gpu_usd_per_hour: 0.34
    video_inputs: {}            # filled in below
```

Then list what the workflow lets you set (this uses no GPU time):

```bash
export COMFYUI_URL=https://<POD_ID>-8188.proxy.runpod.net
uv run python -m app.services.series series/coral-key comfy-check
```

It prints lines such as `6.text = "a cat"   [CLIP Text Encode]`. Map the ones
that matter in `video_inputs`, as `"<node>.<input>": "{{value}}"`:

```yaml
    video_inputs:
      "6.text": "{{prompt}}"        # the prompt box
      "12.image": "{{image}}"       # the Load Image node
      "27.length": "{{frames}}"     # or "{{seconds}}" if the node counts seconds
      "27.width": "{{width}}"
      "27.height": "{{height}}"
      "3.noise_seed": "{{seed}}"
      "40.filename_prefix": "{{prefix}}"
```

The node numbers above are examples: use the ones `comfy-check` prints for your
export. `prompt` and `image` are required; the rest keep the workflow's own
values if you leave them out. Run `comfy-check` again until it shows the
workflow "uses prompt, image, ...".

### 6. Render an episode

```bash
uv run python -m app.services.series series/coral-key plan --episode 1     # if not planned yet
uv run python -m app.services.series series/coral-key render --episode 1 --visuals comfy
```

The episode lands in `storage/series/coral-key/ep01/coral-key-ep01-comfy.mp4`.
`comfy-report.json` beside it lists the GPU seconds and tries per shot and the
cost at your hourly rate. Finished clips are kept, so a rerun only makes the
shots that are missing; `--force` redoes them all.

### 7. Stop the pod

Stop it in RunPod as soon as the render is done. A stopped pod with a network
volume costs only the storage.

## What to look at in the test

- Do Leo and the others still look like their portraits after a few seconds?
- Do the lips match the words, and does each voice suit the character?
- How many shots needed a second try, and what did `comfy-report.json` add up to?

If voices drift from shot to shot, switch to `audio: kokoro`: the clips are made
silent (the prompt asks for a moving mouth) and the local Kokoro voices are laid
over them, as in the stills mode.

## Settings (`render.comfy` in the bible)

| Setting | Default | What it does |
|---|---|---|
| `url` | `COMFYUI_URL`, else `http://127.0.0.1:8188` | The ComfyUI server |
| `video_workflow` | required | API-format export that turns a first frame and a prompt into a clip |
| `video_inputs` | none | Node inputs to replace, so a plain export needs no editing |
| `audio` | `clip` | `clip`: the model speaks. `kokoro`: local voices over silent clips |
| `first_frames` | `workflow` if `still_workflow` is set, else `portrait` | `portrait`: start from the lead's portrait. `workflow`: make a still per shot with `still_workflow`. `gemini`: make it with the Gemini image model |
| `still_workflow`, `still_inputs` | none | Image workflow for first frames. Values: `prompt`, `ref1`..`ref4` (cast portraits), `width`, `height`, `seed`, `prefix` |
| `portrait_workflow`, `portrait_inputs` | none | Image workflow for the cast portraits, instead of Gemini. Values: `prompt`, `width`, `height`, `seed`, `prefix` |
| `width`, `height`, `fps` | 720, 1280, 24 | Passed to the workflow; the result is scaled to 1080x1920 |
| `max_seconds` | 10 | Longest clip to ask for |
| `retries` | 1 | Extra tries per shot, each with a new seed |
| `timeout` | 1800 | Seconds to wait for one job before it is interrupted |
| `gpu_usd_per_hour` | none | Only used to print the cost |

Video workflows receive `prompt`, `image`, `seconds`, `frames` (`seconds x fps + 1`),
`fps`, `width`, `height`, `seed` and `prefix`. You can also type `{{prompt}}`
straight into a text box in ComfyUI before exporting; `video_inputs` exists
because number boxes will not take text.

`COMFYUI_API_KEY`, if set, is sent as a bearer token, for servers behind a proxy
that checks one. ComfyUI itself has no login.
