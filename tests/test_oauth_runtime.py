"""Runtime imports must not trigger Omarchy's installed-plugin file watcher."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeImportTests(unittest.TestCase):
    def test_oauth_import_does_not_create_plugin_bytecode(self):
        with tempfile.TemporaryDirectory(prefix='tt-runtime-') as temporary:
            rig = Path(temporary)
            home = rig / 'home'
            home.mkdir(mode=0o700)
            cli = rig / 'omarchy-ticktick'
            shutil.copy2(ROOT / 'bin/omarchy-ticktick', cli)
            (rig / 'ticktick_oauth.py').write_text(
                'class OAuthError(RuntimeError):\n    code = "denied"\n'
                'def authorize(on_event, timeout=180):\n'
                '    raise OAuthError("Dummy authorization denied")\n')
            env = {'PATH': os.defpath, 'HOME': str(home), 'XDG_STATE_HOME': str(home / 'state')}
            result = subprocess.run([sys.executable, str(cli), 'login', '--oauth', '--json-events'],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('Dummy authorization denied', result.stdout)
            self.assertFalse((rig / '__pycache__').exists(),
                             'Importing OAuth created bytecode under the watched plugin directory')


if __name__ == '__main__':
    unittest.main()
