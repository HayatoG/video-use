import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "render.py"
SPEC = importlib.util.spec_from_file_location("video_use_render", MODULE_PATH)
assert SPEC and SPEC.loader
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class SilentAudioLoudnormTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work = Path(self.temp_dir.name)
        self.source = self.work / "silent.mp4"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=size=320x180:rate=24:duration=0.5",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=0.5",
                "-c:v", "mpeg4", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(self.source),
            ],
            check=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def assert_silent_audio_is_preserved(self, preview: bool):
        output = self.work / ("preview.mp4" if preview else "final.mp4")

        self.assertTrue(render.apply_loudnorm_two_pass(self.source, output, preview=preview))

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "json", str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_type"], "audio")

    def test_final_render_preserves_digital_silence(self):
        self.assert_silent_audio_is_preserved(preview=False)

    def test_draft_render_preserves_digital_silence(self):
        self.assert_silent_audio_is_preserved(preview=True)


if __name__ == "__main__":
    unittest.main()
