# Episodic AI series

The `series` command makes a TikTok-style series with recurring characters. Each
episode comes from a **series bible**: the cast, how each character looks and
sounds, the art style and the season arc. The planner also reads a **recap** of
every earlier episode, so the plot carries on from one episode to the next.

Everything runs locally on your Claude subscription and the offline Kokoro voices.
No paid services are involved.

## Quick start (the included series, *Coral Key*)

```bash
./claude-local.sh --setup-only                  # once: deps, config, Claude login check
uv run --with kokoro-onnx==0.6.1 --with soundfile==0.14.0 \
  python scripts/kokoro_local_server.py &       # local voices (or ./claude-local.sh --local-voice)

uv run python -m app.services.series series/coral-key characters        # draw the cast once
uv run python -m app.services.series series/coral-key plan --episode 1  # Claude writes the episode
uv run python -m app.services.series series/coral-key render --episode 1
# or, each time you want the next episode:
uv run python -m app.services.series series/coral-key next
```

The output for each episode goes to `storage/series/coral-key/epNN/`:

| File | What it is |
|---|---|
| `coral-key-epNN.mp4` | The finished 1080×1920 episode, with voices, subtitles and music |
| `caption.txt` | A TikTok caption with hashtags |
| `episode.json` | The script: scenes, shot descriptions, who says what, and the recap |
| `scenes/sN.svg` / `.png` | One illustration per scene |
| `audio/` | One clip per spoken line, plus the mixed `voice.mp3` |
| `subtitles.srt` | Subtitles timed to each spoken line |

## How it works

1. **Character sheets.** Claude draws each character once, as an SVG model sheet in
   `storage/series/<slug>/characters/`. Every scene prompt includes the sheets of the
   characters in that shot, so faces, hair, outfits and colours stay the same.
2. **Plan.** Claude writes the episode as JSON: 7–9 scenes, each with a shot
   description and 1–4 lines, plus a caption and a recap. It follows the bible's
   format, guardrails and the season-arc beat for that episode number.
3. **Render.** Each scene is drawn as an SVG and rasterised with `resvg`. Each
   line is spoken with that character's voice, and subtitles are timed to the
   real audio length. Stills get a slow zoom, and the regular MoneyPrinterTurbo
   compositor adds subtitles and background music.

Every step reuses files that already exist. To redo one scene, delete
`scenes/sN.png` and run `render` again. Pass `--force` to redo a whole step,
for example `plan --episode 2 --force` to rewrite episode 2.

## Make your own series

Copy `series/coral-key/` to `series/<your-slug>/` and edit `bible.yaml`:

- **`characters`**: `look` should be concrete (hair, outfit, hex colours) because the artist follows it literally. `voice` is any voice name the app supports, such as `kokoro:af_heart` or `en-US-AvaNeural-Female`.
- **`format`**: episode length, scene count and narration style. Keep episodes over 60 seconds if you want TikTok Creator Rewards.
- **`guardrails`**: content rules the writer must follow.
- **`season_arc`**: one line per episode. Later episodes also see the recaps of earlier ones.

Then run `characters` and `next`, and review each episode before posting. Label
AI-generated content when you post it.
