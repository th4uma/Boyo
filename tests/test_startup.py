import unittest
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from boyo.config import Runtime


class StartupTests(unittest.TestCase):
    def test_windows_entry_points_are_ascii_crlf(self):
        repo = Path(__file__).resolve().parents[1]
        for name in ("run.cmd", "启动Boyo.cmd", "repair.cmd"):
            with self.subTest(script=name):
                data = (repo / name).read_bytes()
                data.decode("ascii")
                self.assertIn(b"\r\n", data)
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
                self.assertNotIn(b"\r", data.replace(b"\r\n", b""))

    def test_launcher_uses_repo_relative_local_environment(self):
        repo = Path(__file__).resolve().parents[1]
        data = (repo / "run.cmd").read_text(encoding="ascii")
        self.assertIn('set "BOYO_LOCAL=%~dp0..\\local"', data)
        self.assertIn('"%~dp0launcher.py"', data)
        self.assertIn('environment\\Scripts\\pythonw.exe', data)

    def test_runtime_accepts_previous_environment_and_prefers_current_key(self):
        with patch.dict(os.environ, {'LIUJI_LOCAL': str(Path('previous-runtime').resolve())}, clear=True):
            self.assertEqual(Runtime.default().home, Path('previous-runtime').resolve())
            os.environ['BOYO_LOCAL'] = str(Path('current-runtime').resolve())
            self.assertEqual(Runtime.default().home, Path('current-runtime').resolve())

    def test_previous_environment_stamp_migrates_without_rebuilding_packages(self):
        repo = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('boyo_portable_environment', repo / 'portable_env.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='boyo-upgrade-') as folder:
            local = Path(folder).resolve() / 'local'
            base = local / 'python'
            base.mkdir(parents=True)
            packages = local / 'environment/Lib/site-packages/pip'
            packages.mkdir(parents=True)
            sentinel = packages / 'installed.txt'
            sentinel.write_text('existing packages', encoding='utf-8')
            previous = local / 'environment/.liuji-location'
            previous.write_text(str(local) + '\n', encoding='utf-8')
            with patch.object(sys, 'base_prefix', str(base)), patch('venv.EnvBuilder', side_effect=AssertionError('must reuse environment')):
                self.assertFalse(module.ensure(local))
                self.assertFalse(module.ensure(local))
            self.assertFalse(previous.exists())
            self.assertEqual((local / 'environment/.boyo-location').read_text(encoding='utf-8').strip(), str(local))
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'existing packages')


if __name__ == "__main__":
    unittest.main()
