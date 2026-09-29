"""A word whose timestamps straddle a cut is captioned once, not in both ranges."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import render


class StraddlingWordTests(unittest.TestCase):
    def test_word_across_a_cut_is_captioned_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            edit = Path(tmp)
            (edit / "transcripts").mkdir()
            words = [
                {"type": "word", "text": "vamos", "start": 1.0, "end": 1.5},
                # Folded pause: this word runs across the cut at 2.0.
                {"type": "word", "text": "jogar.", "start": 1.5, "end": 3.5},
            ]
            (edit / "transcripts" / "A.json").write_text(json.dumps({"words": words}))
            edl = {"sources": {"A": "/tmp/A.mov"},
                   "ranges": [{"source": "A", "start": 0.5, "end": 2.0},
                              {"source": "A", "start": 3.0, "end": 4.0}]}
            out = edit / "master.srt"
            render.build_master_srt(edl, edit, out)
            self.assertEqual(out.read_text().upper().count("JOGAR"), 1)


if __name__ == "__main__":
    unittest.main()
