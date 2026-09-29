---
name: video-use
description: Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, burn subtitles — for talking heads, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom.
---

# Video Use

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **30ms audio fades at every segment boundary** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). Otherwise audible pops at every cut.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the Scribe transcript.
7. **Pad every cut edge.** Working window: 30–200ms. Scribe timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `video-use/` project directory.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in `video-use/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached raw Scribe JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    ├── review/                  ← review page + <stem>.review.json + voice/
    ├── preview.mp4
    └── final.mp4
```

## Setup

First-time install lives in `install.md` (clone, deps, ffmpeg, skill registration, API key). Don't re-run it every session; on cold start just verify:

- A transcription key resolves — either in the environment or in `.env` at the video-use repo root. If missing, ask the user to paste one and write it to `.env` (never to the user's `<videos_dir>`). Either provider works:
    - `ELEVENLABS_API_KEY` → `transcribe.py` (Scribe). Tags audio events: `(laughter)`, `(applause)`, `(sigh)`.
    - `DEEPGRAM_API_KEY` → `transcribe_deepgram.py` (nova-3). Same on-disk schema, so everything downstream is identical, and it diarizes. But it returns **no audio-event tokens**, so the `(laughs)`/`(applause)` beat signals in *Cut craft* are unavailable — lean on silence gaps and `timeline_view` instead. Prefer Scribe for reaction-heavy or multi-speaker material where audio events carry the beats.

- **Establish what language is actually spoken before transcribing a batch.** Transcribe one clip, read it against a frame, then run the rest. A wrong `--language` does not error or report low confidence — it returns fluent, grammatical nonsense and silently drops words. For code-switched speech (e.g. Hindi and English alternating mid-sentence) pass `--language multi`; `detect_language` cannot help, because it commits to a single language per file. This matters beyond captions: the cut is reasoned from the transcript, so a language mismatch corrupts the edit itself.
- `python helpers/check_env.py --videos-dir <videos_dir>` passes. Add `--require manim`, `--require remotion`, or `--require hyperframes` when the session will use that backend.
- `ELEVENLABS_API_KEY` resolves — either in the environment or in `.env` at the video-use repo root. If missing, ask the user to paste one and write it to `.env` (never to the user's `<videos_dir>`).
- `ffmpeg` + `ffprobe` on PATH.
- Python deps installed (`uv sync` or `pip install -e .` inside the repo).
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never at the video-use repo root. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.
- This skill vendors `skills/manim-video/`. Read its SKILL.md when building a Manim slot.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file — the skill is typically symlinked at `~/.claude/skills/video-use/` or `~/.codex/skills/video-use/`.

## Helpers

- **`narrate.py <script> -o <base>`** — generate ElevenLabs narration with word timing, SRT sidecars and verified output caching. See `references/narration.md`.
- **`sheet.py`** — build native-frame contact sheets and cached full-resolution reviews. See [frame review](references/frames.md).
- **`song_scan.py`, `identify_track.py`, `motion_audio.py`** — measure music timing, compare supplied recordings, and export audio controls. See [audio analysis](references/audio-analysis.md).
- **`mix_audio.py`, `map_transcript.py`** — mix independent audio tracks and map intact words onto the sample clock. See [audio mixing](references/audio-mixing.md).
- **`visuals.py`, `effects.py`, `track_mask.py`** — compose canvas treatments and layers and track reviewed masks. See [effects and masks](references/effects.md).

- **`caption_raster.py`, `cards.py`** — render styled caption images and measured word cards. See [caption rendering](references/caption-rendering.md).

- **`source_scan.py`, `prepare_source.py`, `find_shot.py`, `project_state.py`** — inspect selected sources and retain provenance. See [source inspection](references/sources.md).
- **`fetch_asset.py image|logo|emoji`** — acquire still assets with source metadata and protected outputs. See `references/assets.md`.
- **`web_shot.py capture|card`** — capture webpage evidence and prepare transparent image cards. See `references/assets.md`.
- **`music_bed.py -o <file.wav>`** — create a simple ambient background loop when the user wants locally generated music without external recordings or paid services. Supports tempo, key and seed controls. Listen before use, then mix separately. See `references/music.md`.
- **`motion_slot.py init|asset|check|render`** — create isolated browser-animation projects for product demos, kinetic typography and motion graphics, then verify the encoded MP4. See `references/slots.md`.

- **`context_router.py --need cuts --need speech`** — select the guidance needed for this task; repeat `--need` for captions, color, animation or sound. Add `--edl <file>` after planning. Read only the returned references; this core remains required. [Selection and receipts](references/context-routing.md).
- **`transcribe.py <video>`** — single-file Scribe call. `--num-speakers N` optional. Cached.
- **`transcribe_deepgram.py <video>`** — Deepgram nova-3 alternative to the above, for anyone who already has a Deepgram key. Emits the identical `{words:[{type,text,start,end,speaker_id}]}` schema, so `pack_transcripts.py` and `render.py --build-subtitles` consume it unchanged. `--language multi` for code-switched speech; `--convert <response.json>` maps an existing response offline with no API call. Cached per source **and** per provider/model/language. **No audio-event tags** — see Setup.
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription. Use for multi-take.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted) → subtitles LAST. `--preview` for 720p fast. `--build-subtitles` to generate master.srt inline. `--height` sets the output height (default 1080) and `--crf` the extract quality (default 16 final / 22 preview) — see *Output quality* below. `--vertical` renders a 9:16 canvas (per-range `layout`: `blur_pad` default, or `split_stack` for a genuine side-by-side two-shot). `--music <file> --duck-level <dB>` mixes a looped bed that ducks under speech.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.
- **`review.py <video>`** — writes a browser page next to the cut where the user scrubs and leaves timecode-anchored comments (`cut` / `shorten` / `lengthen` / `wrong` / free text / voice), optionally pinned to a spot inside the frame by clicking the picture. `--dump <notes.json>` prints them back as time-ordered markdown, transcribing voice notes. No server: the page is opened from disk. Chrome/Edge save to disk directly; other browsers fall back to a download.

For animations, create `<edit>/animations/slot_<id>/` with `Bash` and spawn a sub-agent via the `Agent` tool.

## The process

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Apply grade per-segment. Compose via `render.py`.
6. **Preview.** `render.py --preview`.
7. **Self-eval (before showing the user).** Run `timeline_view` on the **rendered output** (not the sources) at every cut boundary (±1.5s window). Check each image for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike at the boundary (audio pop that slipped past the 30ms fade)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence. Run `ffprobe` on the output to verify duration matches the EDL expectation.

   Measure the audio, don't assume it: `ffmpeg -i out.mp4 -af ebur128=peak=true -f null -` for integrated loudness and true peak, plus RMS per section (dialogue, music-only, end card). An end card 15 dB under the dialogue, or effects louder than speech, is a bug. You cannot listen: say so, and report the numbers.

   For anything the user will publish (launch, promo, ad), also spawn one **critic sub-agent** with the rendered file, the EDL, and any reference videos the user gave. Brief it to roast, not to praise: a verdict, ranked problems with timecodes and evidence (frames, levels), and the 5 fixes to do first. Fresh eyes catch what the author stopped seeing — cut-off payoff lines, 0.5s memes, unreadable 28px text at phone size.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Review (when the user wants to comment).** `review.py <output>` opens a page where they scrub and drop comments on exact timecodes, by voice if they prefer. `review.py --dump <notes>` brings them back as markdown, each with a frame number and, where the user pointed at the picture, the spot as a percentage of the frame. Translate them into EDL changes yourself — the notes are notes, nothing is applied automatically. Worth offering on anything long enough that "the bit about halfway through chapter four" stops being a usable address.
9. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Cut craft (techniques)

Read [the cuts guide](references/context/cuts.md) when this capability is needed.

## The packed transcript (primary reading view)

Read [the speech guide](references/context/speech.md) when this capability is needed.

## Color grade (when requested)

Read [the color guide](references/context/color.md) when this capability is needed.

## Subtitles (when requested)

Subtitles have three dimensions worth reasoning about: **chunking** (1/2/3/sentence per line), **case** (UPPER/Title/Natural), and **placement** (margin from bottom). The right combo depends on content.

**Worked styles** — pick, adapt, or invent:

**`bold-overlay`** — short-form tech launch, fast-paced social. ~2-word chunks, UPPERCASE, break on punctuation and pauses ≥ 0.3s, grow to 3 words rather than flash a cue < 0.35s (`chunk_words` in `render.py`), Helvetica 18 Bold, white-on-outline, `MarginV=90`. `render.py` ships with this as `SUB_FORCE_STYLE`.

```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=90
```

**`natural-sentence`** (if you invent this mode) — narrative, documentary, education. 4–7 word chunks, sentence case, break on natural pauses, `MarginV=60–80`, larger font for readability, slightly wider max-width. No shipped force_style — design one if you need it.

Invent a third style if neither fits. Hard rules: subtitles LAST (Rule 1), output-timeline offsets (Rule 5).

**Driving it from the EDL.** `render.py --build-subtitles` reads an optional `subtitle_style` block, so a style is data rather than a code edit:

```json
"subtitle_style": {
  "words_per_chunk": 6,
  "break_on": ".!?",
  "balance": true,
  "case": "sentence",
  "force_style": "FontName=Helvetica,FontSize=13,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=28"
}
```

Without `words_per_chunk` / `break_on` / `balance` / `min_words`, chunking stays phrase-aware (`chunk_words`); any of them switches to fixed-size cues split at `break_on`. `case` is `"upper"` (default) or `"sentence"` (keeps ASR capitalization, and capitalizes a cue that opens a sentence after a cut). `force_style` replaces `SUB_FORCE_STYLE`; its `MarginV` is relative to `PlayResY=288` — the default 90 is tuned for vertical video, around 28 sits 10% up on 16:9.

Read [the captions guide](references/context/captions.md) when this capability is needed.

## Animations (when requested)

Read [the animation guide](references/context/animation.md) when this capability is needed.

## Music and sound effects (when requested)

Read [the sound guide](references/context/sound.md) when this capability is needed.

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` preserves the first segment source's frame rate (falling back to 24 fps when probing fails); use `--fps` to override it. It scales to 1080p (720p in draft mode); pass `--height` for other targets (e.g. `--height 2160` to deliver at a 4K source's own resolution). Width follows the source aspect, so `--height` is the only resolution knob you need — do not hand-edit the extract command. `--filter` belongs to `grade.py`, not `render.py`. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`grade` is a preset name or raw ffmpeg filter. `overlays` are rendered animation clips. `subtitles` is optional and applied LAST.

`audio_filter` is an optional global audio chain (denoise, EQ) applied per segment **before** the fades, so the fades stay on the true segment edges (Hard Rule 3).

Optional per-range keys:
- `fade_in` / `fade_out` (seconds, default 0.03) — lengthen a range's audio fades for a softer transition. Never go below 0.03 (Hard Rule 3).
- `subtitles: false` — no captions for this range when `--build-subtitles` builds the SRT (music-only beats, title cards).
- `zoom` (≥ 1.0) with `zoom_x` (0–1, default 0.45) — a push-in to disguise jump cuts on a static single-camera shot. A plain number rather than a filter so one EDL stays correct at every output resolution.
- `filter` — raw per-segment ffmpeg escape hatch. A hardcoded `crop` is only valid at one output height, and **any per-segment dimension mismatch breaks the lossless concat** (Hard Rule 2).
- `layout` / `split_faces` — only with `--vertical`.

## Output quality

The video is encoded **twice**: once per segment on extract, then again to composite overlays and burn subtitles. The concat copies video, so it is lossless. This means the **extract CRF is the quality ceiling** — the composite encode can only add loss on top of it, never recover detail.

- `--crf` sets that ceiling. Defaults: 16 final, 22 `--preview`, 28 `--draft`. The composite encode is derived as `crf - 2`.
- `--height` sets the output height; the default 1080 downscales a 4K source and throws away three quarters of its pixels. Pass `--height 2160` to deliver at the source resolution.

If someone reports the output looking soft or compressed, check **bits per pixel**, not bitrate: a 50 Mbps 4K source and a 12.7 Mbps 1080p render are both ≈0.25 bits/px, which means the loss came from discarded pixels and stacked generations rather than bitrate starvation.

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Running Whisper locally on CPU.** Slow and it normalizes fillers. Use hosted Scribe.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Unverified web fonts.** A failed load silently falls back to a system face. Assert the font loaded before rendering.
- **Stock SFX on every transition.** Tie each effect to a visible event; cap the count.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
