"""Transcribe a video on-device with Apple's SpeechTranscriber (macOS 26+).

No API key, no upload: runs Apple's speech model on the Mac's Neural Engine.
Extracts mono 16kHz audio via ffmpeg, runs the Swift helper next to this file
(compiled once into ~/.cache/video-use/), and writes Scribe-shaped JSON to
<edit_dir>/transcripts/<video_stem>.json so pack_transcripts.py and render.py
work unchanged.

Differences from Scribe: no speaker diarization (every word is speaker_0) and
no audio events. Fillers are usually kept, but the model may normalize some.

Cached: if the output file already exists, transcription is skipped.

Usage:
    python helpers/transcribe_apple.py <video_path>
    python helpers/transcribe_apple.py <video_path> --language en-US
    python helpers/transcribe_apple.py <video_path> --edit-dir /custom/edit
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

SWIFT_SOURCE = Path(__file__).resolve().parent / "transcribe_apple.swift"
CACHE_DIR = Path.home() / ".cache" / "video-use"


def build_binary() -> Path:
    """Compile the Swift helper once per source revision."""
    if platform.system() != "Darwin":
        sys.exit("transcribe_apple.py needs macOS 26+ (Apple's SpeechTranscriber)")
    digest = hashlib.sha256(SWIFT_SOURCE.read_bytes()).hexdigest()[:12]
    binary = CACHE_DIR / f"transcribe_apple-{digest}"
    if binary.exists():
        return binary
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"compiling {SWIFT_SOURCE.name} (one-time)")
    subprocess.run(
        ["swiftc", "-O", "-parse-as-library", str(SWIFT_SOURCE), "-o", str(binary)],
        check=True,
    )
    return binary


def extract_audio(video_path: Path, dest: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-map", "0:a:0",
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def transcribe(video_path: Path, edit_dir: Path, language: str) -> Path:
    out_path = edit_dir / "transcripts" / f"{video_path.stem}.json"
    if out_path.exists():
        print(f"cached: {out_path}")
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    binary = build_binary()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        print(f"extracting audio from {video_path.name}")
        extract_audio(video_path, wav)
        print(f"transcribing on-device ({language})")
        subprocess.run([str(binary), str(wav), language, str(out_path)], check=True)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="On-device transcription with Apple SpeechTranscriber")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument("--edit-dir", type=Path, default=None, help="Output directory (default: <video_dir>/edit)")
    ap.add_argument("--language", default="pt-BR", help="Locale, e.g. pt-BR, en-US (default: pt-BR)")
    args = ap.parse_args()

    video_path = args.video.resolve()
    if not video_path.exists():
        sys.exit(f"file not found: {video_path}")
    edit_dir = args.edit_dir.resolve() if args.edit_dir else video_path.parent / "edit"
    transcribe(video_path, edit_dir, args.language)


if __name__ == "__main__":
    main()
