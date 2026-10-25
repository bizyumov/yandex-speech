import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from yandex_speech.artifacts import atomic_write, private_job_dir
from yandex_speech.errors import SpeechError


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_existing_output_and_symlink_target_are_preserved(self):
        path = self.root / "transcript.txt"
        path.write_text("sentinel")
        with self.assertRaises(SpeechError):
            atomic_write(path, b"replacement")
        self.assertEqual(path.read_text(), "sentinel")
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(SpeechError):
            atomic_write(link, b"replacement", overwrite=True)
        self.assertEqual(path.read_text(), "sentinel")

    def test_atomic_publish_has_one_winner_without_partial_data(self):
        path = self.root / "out.txt"
        def write(value):
            try:
                atomic_write(path, value)
                return True
            except SpeechError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, [b"a" * 10000, b"b" * 10000]))
        self.assertEqual(sum(outcomes), 1)
        self.assertIn(path.read_bytes(), [b"a" * 10000, b"b" * 10000])
        self.assertEqual(sorted(x.name for x in self.root.iterdir()), ["out.txt"])

    def test_owned_job_directories_are_unique_and_private(self):
        a = private_job_dir(self.root)
        b = private_job_dir(self.root)
        self.assertNotEqual(a, b)
        self.assertEqual(a.stat().st_mode & 0o777, 0o700)

    def test_explicit_overwrite_publishes_complete_replacement(self):
        path = self.root / "out.txt"
        path.write_text("old")
        atomic_write(path, b"new", overwrite=True)
        self.assertEqual(path.read_text(), "new")
