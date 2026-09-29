"""Analyze BPM and beat positions from an audio/video file.

Uses librosa for beat tracking. Outputs a JSON file with BPM and
beat timestamps that render.py or the video-use workflow can use
to align cuts to the music.

Usage:
    python helpers/analyze_beats.py input/music/track.mp3
    python helpers/analyze_beats.py input/music/track.mp3 --edit-dir edit/
    python helpers/analyze_beats.py input/music/track.mp3 --every 2  # every 2nd beat
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def extract_audio(source: Path, tmp_wav: Path) -> None:
    """Extract mono 22050 Hz WAV from any video/audio file via ffmpeg."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(source),
            "-ac", "1", "-ar", "22050",
            "-vn", str(tmp_wav),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def analyze(source: Path, every_n: int = 1) -> dict:
    """Return BPM and beat timestamps (optionally every Nth beat)."""
    try:
        import librosa  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        sys.exit(
            "librosa is not installed. Run: pip install librosa\n"
            "If you also need numpy: pip install librosa numpy"
        )

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_wav = Path(f.name)

    try:
        print(f"extracting audio from {source.name} …")
        extract_audio(source, tmp_wav)

        print("analyzing beats …")
        y, sr = librosa.load(str(tmp_wav), sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()

        # Apply every-N filter
        if every_n > 1:
            beat_times = beat_times[::every_n]

        bpm = float(np.round(tempo, 1))
        duration = float(librosa.get_duration(y=y, sr=sr))

        return {
            "source": source.name,
            "bpm": bpm,
            "duration_sec": round(duration, 2),
            "beat_count": len(beat_times),
            "every_n_beats": every_n,
            "beat_times": [round(t, 3) for t in beat_times],
        }
    finally:
        tmp_wav.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze BPM and beat positions")
    ap.add_argument("source", type=Path, help="Audio or video file to analyze")
    ap.add_argument(
        "--edit-dir", type=Path, default=None,
        help="Output directory for beats JSON (default: <source_parent>/edit/beats/)",
    )
    ap.add_argument(
        "--every", type=int, default=1, metavar="N",
        help="Keep every Nth beat (e.g. --every 2 for half-time feel). Default: 1",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="Print result as JSON to stdout instead of saving to file",
    )
    args = ap.parse_args()

    source = args.source.resolve()
    if not source.exists():
        sys.exit(f"file not found: {source}")

    result = analyze(source, every_n=args.every)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    # Save to edit/beats/<stem>_beats.json
    edit_dir = args.edit_dir or source.parent / "edit"
    beats_dir = edit_dir / "beats"
    beats_dir.mkdir(parents=True, exist_ok=True)
    out_path = beats_dir / f"{source.stem}_beats.json"
    out_path.write_text(json.dumps(result, indent=2))

    print(f"\nBPM: {result['bpm']}")
    print(f"Duration: {result['duration_sec']}s")
    print(f"Beats found: {result['beat_count']} (every {result['every_n_beats']})")
    print(f"First 8 beat positions: {result['beat_times'][:8]}")
    print(f"\nsaved → {out_path}")


if __name__ == "__main__":
    main()
