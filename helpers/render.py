"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Concat sample-exact PCM segments into base.mp4 (copied video, one AAC encode)
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=90).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

# Console output uses arrows (→) below. On Windows, stdout's default
# encoding is the legacy console codepage (e.g. cp1252), which can't encode
# them and raises UnicodeEncodeError. Force UTF-8 regardless of platform or
# locale so the same prints work unmodified on Windows, macOS, and Linux.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
SUB_FORCE_STYLE = (
    # Helvetica isn't installed on Windows, so libass/fontconfig had to
    # guess a substitute (Arial-BoldMT) at render time. Arial ships as a
    # core system font on Windows and macOS, so naming it directly removes
    # that guesswork on both. Linux distros without Arial still get a
    # graceful fontconfig substitution, same as before.
    "FontName=Arial,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def probe_duration(path: Path) -> float:
    """Container duration of a rendered file, in seconds."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`.

    Sources live next to (not inside) the edit dir and the EDL documents
    absolute paths, so neither form may be rejected here.
    """
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def resolve_subtitles_path(maybe_path: str, edit_dir: Path) -> Path:
    """Resolve the EDL's subtitles path: relative to the EDL's directory, else the
    current directory (agents often write "edit/master.srt"). A missing file is an
    error: rendering on without it silently ships a video with no captions."""
    candidates = [resolve_path(maybe_path, edit_dir)]
    if not Path(maybe_path).is_absolute():
        candidates.append(Path(maybe_path).resolve())
    for c in candidates:
        if c.exists():
            return c
    tried = ", ".join(str(c) for c in candidates)
    sys.exit(f"subtitles file in EDL not found (tried {tried}). Fix the path or pass --no-subtitles.")


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.
#
# The transfer is read from the first decoded frame, not only the stream
# header: some cameras write bt2020-10 in the header and signal HLG in the
# alternative-transfer SEI, which ffmpeg reports on decoded frames. Clips that
# went through a messaging app or an editor can also lose some or all tags.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG
# Rec.709-family and sRGB curves: a clip tagged with one of these is SDR, even
# when it is 10-bit or BT.2020.
SDR_TRANSFERS = {
    "bt709", "smpte170m", "bt470m", "bt470bg", "smpte240m",
    "bt2020-10", "bt2020-12", "iec61966-2-1",
}
COLOR_FIELDS = ("color_transfer", "color_primaries", "color_space", "pix_fmt")

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def _probe_color_entries(video: Path, section: str, extra: list[str]) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", *extra,
             "-show_entries", f"{section}={','.join(COLOR_FIELDS)}",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        return (json.loads(out.stdout).get(f"{section}s") or [{}])[0]
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return {}


def probe_color(video: Path) -> dict[str, str | None]:
    """Colour fields of the first decoded frame, falling back to the stream header.

    The `0%` seek in -read_intervals matters: without it a stream-copied trim
    with an edit list returns no frame.
    """
    frame = _probe_color_entries(video, "frame", ["-read_intervals", "0%+#1"])
    stream = _probe_color_entries(video, "stream", [])

    def known(value: str | None) -> bool:
        return value not in (None, "", "unknown", "unspecified", "reserved")

    return {
        field: frame.get(field) if known(frame.get(field))
        else stream.get(field) if known(stream.get(field)) else None
        for field in COLOR_FIELDS
    }


@functools.lru_cache(maxsize=None)
def _pix_fmt_bit_depths() -> dict[str, int]:
    """Deepest component of every pixel format, from the `ffprobe -pix_fmts` table."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-pix_fmts"],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return {}
    depths = {}
    for line in out.splitlines():
        # FLAGS NAME NB_COMPONENTS BITS_PER_PIXEL BIT_DEPTHS, e.g. "IO... gray10le 1 10 10"
        m = re.fullmatch(r"\S{5}\s+(\S+)\s+\d+\s+\d+\s+(\d+(?:-\d+)*)", line.strip())
        if m:
            depths[m[1]] = max(int(d) for d in m[2].split("-"))
    return depths


def _is_high_bit_depth(pix_fmt: str) -> bool:
    depth = _pix_fmt_bit_depths().get(pix_fmt)
    if depth is not None:
        return depth > 8
    # ffprobe older than 5.0 prints no BIT_DEPTHS column: read the depth from the name.
    return bool(re.search(r"p1[0-6]|^p01[06]|(?:gray|rgb|bgr)1[0-6]", pix_fmt))


def tonemap_filter(video: Path) -> str | None:
    """The HDR → SDR chain for a PQ or HLG source, or None for SDR.

    Exits when a 10-bit or BT.2020 clip has no HDR or SDR transfer to say which it is.
    """
    color = probe_color(video)
    transfer = color["color_transfer"]
    if transfer in HDR_TRANSFERS:
        # zscale reads the input transfer, primaries and matrix from the frame.
        # Pin the probed transfer and fill missing primaries/matrix with the
        # BT.2020 values every HLG/PQ camera uses.
        primaries = color["color_primaries"] or "bt2020"
        matrix = color["color_space"] or "bt2020nc"
        return (f"setparams=color_trc={transfer}:color_primaries={primaries}"
                f":colorspace={matrix}," + TONEMAP_CHAIN)
    if transfer in SDR_TRANSFERS:
        return None
    pix_fmt = color["pix_fmt"] or ""
    wide = any("bt2020" in (color[f] or "") for f in ("color_primaries", "color_space"))
    if wide or _is_high_bit_depth(pix_fmt):
        hint = "BT.2020" if wide else pix_fmt
        sys.exit(
            f"{video.name}: {hint} with transfer {transfer or 'untagged'}, so it may be HLG, PQ, SDR "
            "or log footage. Ask what it was recorded as, then re-tag it without re-encoding, e.g. "
            "ffmpeg -i in.mov -c copy -bsf:v hevc_metadata=transfer_characteristics=18 out.mov "
            "(18 HLG, 16 PQ, 1 SDR; h264_metadata for H.264)."
        )
    return None


@functools.lru_cache(maxsize=None)
def _source_tonemap_filter(video: Path) -> str | None:
    """tonemap_filter once per source: a render cuts many segments from the same file."""
    return tonemap_filter(video)


def is_hdr_source(video: Path) -> bool:
    """True when the source needs the HDR → SDR chain (see tonemap_filter)."""
    return _source_tonemap_filter(video.resolve()) is not None


def get_source_dims(video: Path) -> tuple[int, int]:
    """Return (width, height) of the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )
    w, h = map(int, out.stdout.strip().split(","))
    return w, h


def is_portrait_source(video: Path) -> bool:
    """Return True if the displayed video is portrait, including rotation."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=width,height:stream_side_data=rotation",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return False
        stream = streams[0]
        w, h = int(stream["width"]), int(stream["height"])

        # ffmpeg autorotates display-matrix side data before applying filters.
        # Swap coded dimensions for quarter-turns so the scale axis is selected
        # from the dimensions the filter actually sees. A plain metadata tag is
        # intentionally ignored because it does not guarantee autorotation.
        rotation = 0
        for side_data in stream.get("side_data_list") or []:
            if side_data.get("rotation") is not None:
                rotation = side_data["rotation"]
                break
        if int(round(float(rotation))) % 360 in (90, 270):
            w, h = h, w
        return h > w
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        OSError,
        OverflowError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False


def parse_fps(value: str) -> str:
    """Validate and canonicalize an ffmpeg frame rate."""
    text = value.strip()
    if len(text) > 32 or not re.fullmatch(
        r"(?:[0-9]+(?:\.[0-9]+)?|[0-9]+/[0-9]+)", text
    ):
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        )
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        ) from exc
    if rate <= 0:
        raise argparse.ArgumentTypeError("FPS must be greater than zero")
    # FFmpeg stores video rates as AVRational (signed 32-bit components).
    # Bounding the reduced fraction keeps every accepted canonical value safe
    # for ffmpeg and makes parse_fps(parse_fps(value)) idempotent.
    max_component = 2_147_483_647
    if rate.numerator > max_component or rate.denominator > max_component:
        raise argparse.ArgumentTypeError("FPS precision or magnitude is too large")
    return f"{rate.numerator}/{rate.denominator}"


def probe_source_fps(video: Path) -> str | None:
    """Return an ffmpeg-ready source rate, preferring the average frame rate.

    ``avg_frame_rate`` represents the observed average and is the better default
    for variable-frame-rate inputs. ``r_frame_rate`` remains a fallback for
    streams where the average is unavailable. Values are normalized to an exact
    rational so rates such as ``30000/1001`` survive without rounding.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return None
        for field in ("avg_frame_rate", "r_frame_rate"):
            value = streams[0].get(field)
            if value and value != "0/0":
                try:
                    return parse_fps(value)
                except argparse.ArgumentTypeError:
                    continue
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    return None


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    rate: str | None = None,
    portrait: bool | None = None,
    hdr: bool | None = None,
    vertical: bool = False,
    layout: str = "blur_pad",
    split_faces: list | None = None,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.

    `vertical=True` converts any source orientation to a 1080x1920 (or 720x1280
    for draft) canvas. Two layouts:

      - "blur_pad" (default): the full original frame is scaled to fit inside
        the canvas untouched (no cropping — text overlays and full-frame shots
        stay intact), with a blurred/cropped copy of the same frame filling the
        letterbox bars top/bottom (landscape source) or sides (narrower-than-9:16
        source). Use for anything that is NOT a genuine side-by-side split-screen
        (single-subject shots, graphics/cards, B-roll) — cropping those would
        cut off content for no benefit.
      - "split_stack": for a genuine left/right split-screen (e.g. two hosts).
        Crops the left half and right half of the frame and stacks them full-
        width, top and bottom — uses the full 1080 width for each speaker
        instead of shrinking both into a blurred letterbox. Only use this for
        segments that are actually split-screen for their whole duration;
        applying it to single-shot footage will crop content arbitrarily.

        `split_faces` refines the split_stack crops: a list of two normalized
        (x, y) face centers in the FULL source frame, [[left_cx, left_cy],
        [right_cx, right_cy]]. Each panel's crop window is centered
        horizontally on its face and places the face at ~42% of the panel
        height (rule of thirds) instead of a blind center crop. Read the face
        positions off a frame with timeline_view / ffmpeg before setting them.
        Static crop — correct for locked-off studio shots; it does not track
        a face that walks across the frame.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # `portrait` and `hdr` depend only on the source file, never on the cut
    # range. extract_all_segments probes each source once and passes the answers
    # in; probe here only when called standalone.
    if portrait is None:
        portrait = is_portrait_source(source)
    long_edge = 1280 if draft else 1920

    if hdr is None:
        hdr = is_hdr_source(source)
    tonemap = (_source_tonemap_filter(source.resolve()) or TONEMAP_CHAIN) if hdr else ""

    if vertical:
        w, h = (long_edge * 9) // 16, long_edge
        pre = f"{tonemap}," if tonemap else ""
        if layout == "split_stack":
            half_h = h // 2
            src_w, src_h = get_source_dims(source)
            panel_w = src_w / 2
            aspect = w / half_h  # 1080/960 = 1.125
            crop_w = min(panel_w, src_h * aspect)
            crop_h = crop_w / aspect
            faces = split_faces or [[0.25, 0.5], [0.75, 0.5]]

            def _panel_crop(i: int) -> tuple[int, int, int, int]:
                panel_x0 = 0.0 if i == 0 else src_w / 2
                fcx = faces[i][0] * src_w
                fcy = faces[i][1] * src_h
                x = max(panel_x0, min(fcx - crop_w / 2, panel_x0 + panel_w - crop_w))
                # Face at ~42% of the crop height (rule of thirds), not dead center
                y = max(0.0, min(fcy - 0.42 * crop_h, src_h - crop_h))
                # Even coords for yuv420 chroma alignment
                return (int(crop_w) // 2 * 2, int(crop_h) // 2 * 2,
                        int(x) // 2 * 2, int(y) // 2 * 2)

            lw, lh, lx, ly = _panel_crop(0)
            rw, rh, rx, ry = _panel_crop(1)
            filter_complex = (
                f"[0:v]{pre}split=2[left_src][right_src];"
                f"[left_src]crop={lw}:{lh}:{lx}:{ly},scale={w}:{half_h}[top];"
                f"[right_src]crop={rw}:{rh}:{rx}:{ry},scale={w}:{half_h}[bottom];"
                f"[top][bottom]vstack=inputs=2"
            )
        else:
            # Blur-pad: full frame fit inside the canvas (no crop) over a
            # blurred, cropped-to-fill copy of the same frame as background.
            filter_complex = (
                f"[0:v]{pre}split=2[bg_src][fg_src];"
                f"[bg_src]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},gblur=sigma=25[bg];"
                f"[fg_src]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
            )
        if grade_filter:
            filter_complex += f",{grade_filter}"
        filter_complex += "[outv]"
    else:
        scale = "scale=-2:1920" if portrait else "scale=1920:-2"
        if draft:
            scale = "scale=-2:1280" if portrait else "scale=1280:-2"
        vf_parts: list[str] = []
        if tonemap:
            vf_parts.append(tonemap)
        vf_parts.append(scale)
        if grade_filter:
            vf_parts.append(grade_filter)
        vf = ",".join(vf_parts)

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    # Frame rate: use the rate the caller resolved once for the whole render
    # (every segment must share it — concat -c copy in Rule 2 requires a uniform
    # frame rate). When called standalone with no rate, preserve this source's
    # own rate; fall back to 24 only if it can't be probed.
    out_rate = rate if rate is not None else (probe_source_fps(source) or "24")

    # Quantize the segment to whole output frames, then force the audio to the
    # exact same duration (PCM intermediates are sample-exact). Otherwise video
    # rounds up to a whole frame while audio keeps the raw -t length, and every
    # join gets a 17-40ms mismatch (upstream PR #62 measured -0.57s of drift
    # over 37 segments before the concat re-encoded audio).
    n_frames = max(1, round(duration * Fraction(out_rate)))
    vdur = float(n_frames / Fraction(out_rate))

    # 30ms audio fades at both edges (Rule 3) — prevent pops
    fade_out_start = max(0.0, vdur - 0.03)
    af = (
        f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03,"
        f"atrim=end={vdur:.6f},apad=whole_dur={vdur:.6f}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        # -t overshoots so the audio filters have enough input to atrim/apad to
        # exactly vdur; video is capped by -frames:v instead.
        "-t", f"{vdur + 0.5:.3f}",
        "-frames:v", str(n_frames),
    ]
    if vertical:
        cmd += ["-filter_complex", filter_complex, "-map", "[outv]", "-map", "0:a"]
    else:
        cmd += ["-vf", vf]
    cmd += [
        "-af", af,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", out_rate,
        "-c:a", "pcm_s16le", "-ar", "48000",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fps: str | None = None,
    vertical: bool = False,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mov (PCM audio).
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    # Resolve ONE output frame rate for the entire render and apply it to every
    # segment. The lossless concat (Rule 2, `-c copy`) requires all segments to
    # share a frame rate; probing per-segment would diverge for multi-source
    # EDLs that mix rates (e.g. a 30fps and a 60fps source) and break the concat.
    # Explicit --fps wins; otherwise preserve the first source's rate.
    if fps is not None:
        out_rate = parse_fps(str(fps))
    elif ranges:
        first_src = resolve_path(sources[ranges[0]["source"]], edit_dir)
        out_rate = probe_source_fps(first_src) or "24"
    else:
        out_rate = "24"

    # One orientation + HDR probe per distinct source, not per range. A 40-range
    # EDL over 2 sources drops from 80 ffprobe calls to 4.
    source_facts: dict[Path, tuple[bool, bool]] = {}

    def facts_for(path: Path) -> tuple[bool, bool]:
        if path not in source_facts:
            source_facts[path] = (is_portrait_source(path), is_hdr_source(path))
        return source_facts[path]

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/  @ {out_rate} fps"
          f"{' (forced)' if fps is not None else ' (from source)'}")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mov"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        portrait, hdr = facts_for(src_path)
        extract_segment(
            src_path, start, duration, seg_filter, out_path,
            preview=preview, draft=draft, rate=out_rate,
            portrait=portrait, hdr=hdr,
            vertical=vertical, layout=r.get("layout", "blur_pad"),
            split_faces=r.get("split_faces"),
        )
        seg_paths.append(out_path)

    return seg_paths


# -------- Concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Copy video and re-encode audio to avoid AAC priming clicks at segment joins."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    # Escape single quotes for the concat demuxer line format. A literal ' inside
    # a single-quoted path must be written as '\'' (close quote, escaped quote,
    # reopen quote); otherwise a path containing an apostrophe (e.g. a folder
    # named "What I couldn't do") truncates the path and ffmpeg fails to concat.
    def _concat_quote(p: Path) -> str:
        return str(p.resolve()).replace("'", "'\\''")
    concat_list.write_text("".join(f"file '{_concat_quote(p)}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


CHUNK_WORDS = 2         # target words per cue
CHUNK_MAX_WORDS = 3     # a too-short chunk may grow to this many words
CHUNK_MIN_S = 0.35      # a cue shorter than this reads as a flash
CHUNK_PAUSE_S = 0.3     # a gap this long between words ends the cue


def chunk_words(words: list[dict]) -> list[list[dict]]:
    """Group transcript words into caption cues.

    A cue closes on trailing punctuation or on a pause before the next word.
    Otherwise it closes at CHUNK_WORDS words, unless it would be on screen for
    less than CHUNK_MIN_S; then it takes up to CHUNK_MAX_WORDS words. So
    "does | is" across a pause stays split, and fast "what a" does not flash.
    """
    words = [w for w in words if (w.get("text") or "").strip()]
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for i, w in enumerate(words):
        current.append(w)
        text = w["text"].strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        dur = w["end"] - current[0]["start"]
        if (
            nxt is None
            or text[-1] in PUNCT_BREAK
            or gap >= CHUNK_PAUSE_S
            or len(current) >= CHUNK_MAX_WORDS
            or (len(current) >= CHUNK_WORDS and dur >= CHUNK_MIN_S)
        ):
            chunks.append(current)
            current = []
    return chunks


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path,
                     segment_paths: list[Path] | None = None) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - phrase-aware ~2-word chunks (see chunk_words)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset

    `segment_paths`, when given, are the extracted clips in concat order. Their
    measured durations are used for the per-segment offset instead of the EDL's
    `end - start`; see the comment below for why that matters.
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    # The offset has to be where the segment actually STARTS in the concatenated
    # output. An extract is quantised to whole frames, so it is a fraction of a
    # frame longer than `end - start`, and summing the EDL's floats accumulates
    # that error: on a 30-segment, 3m34s edit the captions ran 0.598s early by the
    # final cue. Measure the rendered clips when they are available.
    measured: list[float] | None = None
    if segment_paths:
        if len(segment_paths) != len(edl["ranges"]):
            print(f"  warning: {len(segment_paths)} clips for {len(edl['ranges'])} ranges;"
                  f" falling back to EDL durations for caption offsets")
        else:
            try:
                measured = [probe_duration(p) for p in segment_paths]
                drift = sum(measured) - sum(float(r["end"]) - float(r["start"])
                                            for r in edl["ranges"])
                print(f"  caption offsets from measured segments (drift vs EDL: {drift:+.3f}s)")
            except (subprocess.CalledProcessError, ValueError, OSError) as exc:
                print(f"  warning: could not measure segments ({exc}); using EDL durations")
                measured = None

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for seg_i, r in enumerate(edl["ranges"]):
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = measured[seg_i] if measured else (seg_end - seg_start)

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        for chunk in chunk_words(words_in_seg):
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    # Explicit UTF-8: Path.write_text() otherwise falls back to
    # locale.getpreferredencoding(), which is cp1252 on Windows and would
    # mangle Spanish accents/ñ in the burned-in captions.
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True after writing either a normalized copy or an unchanged copy for
    audio with no finite integrated loudness.

    Preview mode uses a one-pass approximation after measuring only to detect
    digital silence. Final mode uses the measurement for the proper two-pass.
    """
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is not None:
        try:
            input_i = float(measurement["input_i"])
        except (TypeError, ValueError):
            measurement = None
        else:
            if not math.isfinite(input_i):
                print("  audio is silent — skipping loudness normalization")
                shutil.copyfile(input_path, output_path)
                return True

    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")

    if preview or measurement is None:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        mode = "1-pass preview" if preview else "1-pass fallback"
        print(f"  loudnorm ({mode}) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Music mixing with voice ducking ------------------------------------


def mix_music_with_ducking(
    video_path: Path,
    music_path: Path,
    out_path: Path,
    duck_db: float = 18.0,
) -> None:
    """Mix background music into video with sidechaincompress ducking.

    Music is looped if shorter than the video. Voice (from video) ducks the
    music by `duck_db` decibels during speech via sidechaincompress.

    duck_db=12 → subtle (music stays audible under speech)
    duck_db=18 → standard (music clearly recedes under speech)
    duck_db=20+ → strong (music almost disappears under speech)
    """
    # Music weight in mix: 0.3 keeps it clearly background
    music_weight = 0.3
    threshold = 0.02
    attack = 200   # ms — how fast ducking kicks in
    release = 1000  # ms — how fast music comes back

    if duck_db < 0:
        raise ValueError(f"duck_db must be non-negative, got {duck_db}")

    # Map duck_db to sidechaincompress ratio (ffmpeg valid range: 1–20).
    # ratio=1.0 means no compression (duck_db=0 → no ducking effect).
    # duck_db=12 → ratio≈4 (subtle), duck_db=18 → ratio≈9 (standard), duck_db=20 → ratio≈13
    ratio = round(min(20.0, max(1.0, duck_db ** 1.3 / 10.0)), 1)

    filter_complex = (
        # Loop music indefinitely so short tracks cover the full video
        f"[1:a]aloop=loop=-1:size=2e+09,asetpts=N/SR/TB[music_loop];"
        # Sidechain: voice signal compresses music when speech is present
        f"[music_loop][0:a]sidechaincompress="
        f"threshold={threshold}:ratio={ratio}:attack={attack}:release={release}"
        f"[music_ducked];"
        # Mix ducked music with voice
        f"[0:a][music_ducked]amix=inputs=2:duration=first:"
        f"weights=1 {music_weight}[a_out]"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-i", str(music_path),
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[a_out]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"mixing music (ducking -{duck_db} dB under speech) → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        # VP9/WebM overlays carry alpha as a side-data plane. ffmpeg's native vp9
        # decoder drops it (decodes to yuv420p → opaque), which turns a full-frame
        # transparent overlay into an opaque black layer. Force the alpha-aware
        # libvpx-vp9 decoder so overlay alpha is honored.
        if ov_path.suffix.lower() == ".webm":
            inputs += ["-c:v", "libvpx-vp9", "-i", str(ov_path)]
        else:
            inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        # ffmpeg's filtergraph parser treats both ':' and '\' as syntax
        # (option separator / escape char). On Windows, an absolute path
        # like C:\Users\...\master.srt trips both at once — escaping only
        # the colon leaves the raw backslashes to break the parser. Normalize
        # to forward slashes first (ffmpeg accepts them on every platform,
        # including Windows), then escape the drive-letter colon and any
        # literal quotes. POSIX paths have no backslashes, so the replace is
        # a no-op there and this stays identical to the prior behavior.
        subs_abs = str(subtitles_path.resolve()).replace("\\", "/")
        subs_abs = subs_abs.replace(":", r"\:").replace("'", r"\'")
        # filename= (named option) is required: ffmpeg 8's filtergraph parser
        # rejects the positional quoted form when the path needs quoting.
        if subtitles_path.suffix.lower() == ".ass":
            # ASS files carry their own styles — force_style would clobber them.
            filter_parts.append(f"{current}subtitles=filename='{subs_abs}'[outv]")
        else:
            filter_parts.append(
                f"{current}subtitles=filename='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
            )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    # Clamp output to the base duration. overlay's framesync would otherwise
    # extend the output with frozen frames whenever an overlay input outlasts
    # the base video (e.g. a caption layer rendered with a trailing hold).
    base_dur = None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(base_path)],
            capture_output=True, text=True, check=True,
        )
        base_dur = float(out.stdout.strip().splitlines()[0])
    except Exception:
        pass

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        *(["-t", f"{base_dur:.3f}"] if base_dur else []),
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    # Windows consoles often default to cp1252, which can't encode the arrow
    # characters used in progress prints (U+2192) — replace instead of crashing.
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--fps",
        type=parse_fps,
        default=None,
        help="Output frame rate. Default: preserve the source's frame rate "
             "(falls back to 24 if it can't be probed). Pass e.g. --fps 30 or "
             "--fps 30000/1001 to force.",
    )
    ap.add_argument(
        "--vertical",
        action="store_true",
        help="Force 1080x1920 (720x1280 in --draft) output via blur-pad, regardless of "
             "source orientation. Full frame is preserved (no crop) with a blurred "
             "copy of the same frame filling the letterbox bars.",
    )
    ap.add_argument(
        "--music",
        type=Path,
        default=None,
        metavar="MUSIC_FILE",
        help="Background music file to mix in. Loops automatically if shorter than video.",
    )
    ap.add_argument(
        "--duck-level",
        type=float,
        default=18.0,
        metavar="DB",
        help="dB reduction of music under speech (0–40). Default: 18. "
             "12=subtle, 18=standard, 20+=strong.",
    )
    args = ap.parse_args()

    if not (0 <= args.duck_level <= 40):
        sys.exit("--duck-level must be between 0 and 40 dB")

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft, fps=args.fps,
        vertical=args.vertical,
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path, segment_paths)
        elif edl.get("subtitles"):
            subs_path = resolve_subtitles_path(edl["subtitles"], edit_dir)

    # 4. Composite (overlays + subtitles LAST) → intermediate path
    overlays = edl.get("overlays") or []
    music_path = args.music.resolve() if args.music else None
    if music_path and not music_path.exists():
        sys.exit(f"music file not found: {music_path}")

    # Determine intermediate path chain: composite → [music] → [loudnorm] → out
    needs_music = music_path is not None
    needs_loudnorm = not args.no_loudnorm

    if not needs_music and not needs_loudnorm:
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir)
    elif not needs_music and needs_loudnorm:
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)
    elif needs_music and not needs_loudnorm:
        tmp_composite = out_path.with_suffix(".premusic.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        mix_music_with_ducking(tmp_composite, music_path, out_path, duck_db=args.duck_level)
        tmp_composite.unlink(missing_ok=True)
    else:
        # composite → music → loudnorm → final
        tmp_composite = out_path.with_suffix(".premusic.mp4")
        tmp_music = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        mix_music_with_ducking(tmp_composite, music_path, tmp_music, duck_db=args.duck_level)
        tmp_composite.unlink(missing_ok=True)
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_music, out_path, preview=args.draft)
        tmp_music.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
