"""Opt-in real Quickshell OAuth Process tests, isolated from account and desktop.

TICKTICK_NATIVE_TESTS=1 python3 -m unittest discover -s tests -p test_service_oauth_native.py
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('TICKTICK_NATIVE_TESTS') == '1', 'opt-in Omarchy VM native tests')
class NativeOAuthServiceTests(unittest.TestCase):
    def exercise(self, mode):
        with tempfile.TemporaryDirectory(prefix='tt-oauth-') as temporary:
            rig = Path(temporary)
            home = rig / 'home'
            runtime = rig / 'run'
            home.mkdir(mode=0o700)
            runtime.mkdir(mode=0o700)
            (rig / 'bin').mkdir()
            shutil.copy2(ROOT / 'Model.js', rig / 'Model.js')
            shutil.copy2(ROOT / 'Service.qml', rig / 'Service.qml')
            if mode != 'failed-start':
                fake = rig / 'bin' / 'omarchy-ticktick'
                fake.write_text(textwrap.dedent('''\
                    #!/usr/bin/python3
                    import json, os, pathlib, sys, time
                    home = pathlib.Path(os.environ['HOME'])
                    if '--oauth' in sys.argv:
                        (home / 'oauth-child.pid').write_text(str(os.getpid()))
                        print(json.dumps({'phase':'preparing'}), flush=True)
                        mode = os.environ['TEST_OAUTH_MODE']
                        if mode == 'cancel':
                            time.sleep(20)
                        elif mode == 'denied':
                            print(json.dumps({'phase':'error','code':'denied','message':'Dummy authorization denied'}), flush=True)
                            sys.exit(1)
                        else:
                            state = home / '.local/state/omarchy/ticktick'
                            state.mkdir(parents=True, mode=0o700)
                            (state / 'data.json').write_text(json.dumps({'syncedAt':1,'authRequired':False,'tasks':[],'habits':[]}))
                            print(json.dumps({'phase':'validating'}), flush=True)
                            print(json.dumps({'phase':'connected','ok':True}), flush=True)
                    sys.exit(0)
                    '''))
                fake.chmod(0o700)
            (rig / 'shell.qml').write_text(textwrap.dedent('''\
                import QtQuick
                import Quickshell
                ShellRoot {
                  Service { id: service; settings: ({syncInterval: "Manual"}) }
                  Timer { interval: 100; running: true; onTriggered: service.startOAuth() }
                  Timer {
                    interval: 350
                    running: Quickshell.env("TEST_OAUTH_MODE") === "cancel"
                    onTriggered: service.cancelOAuth()
                  }
                  Timer {
                    interval: 1000; running: true
                    onTriggered: {
                      console.log("OAUTH_RESULT " + JSON.stringify({
                        phase: service.authState, error: service.authError,
                        busy: service.oauthConnecting, url: service.authUrl,
                        pendingToken: service.loginToken, signedIn: service.signedIn
                      }))
                      Qt.quit()
                    }
                  }
                }
                '''))
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('HERMES_', 'TICKTICK_'))}
            env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                       XDG_STATE_HOME=str(home / '.local/state'),
                       XDG_DATA_HOME=str(home / '.local/share'), XDG_CACHE_HOME=str(home / '.cache'),
                       XDG_RUNTIME_DIR=str(runtime), QT_QPA_PLATFORM='offscreen',
                       TEST_OAUTH_MODE=mode, PATH='/usr/bin')
            for key in ['WAYLAND_DISPLAY', 'DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE']:
                env.pop(key, None)
            process = subprocess.run(['quickshell', '-n', '-p', str(rig)], env=env,
                                     capture_output=True, text=True, timeout=8)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            lines = [line.split('OAUTH_RESULT ', 1)[1] for line in process.stdout.splitlines()
                     if 'OAUTH_RESULT ' in line]
            if not lines:
                lines = [line.split('OAUTH_RESULT ', 1)[1] for line in process.stderr.splitlines()
                         if 'OAUTH_RESULT ' in line]
            self.assertEqual(len(lines), 1, process.stdout + process.stderr)
            result = json.loads(lines[0])
            self.assertFalse(result['busy'], result)
            self.assertEqual(result['url'], '')
            self.assertEqual(result['pendingToken'], '')
            self.assertFalse((home / '.local/state/omarchy/ticktick/session.json').exists())
            pid_file = home / 'oauth-child.pid'
            if pid_file.exists():
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)
            return result

    def test_native_success_reloads_the_cache(self):
        result = self.exercise('success')
        self.assertEqual(result['phase'], 'idle')
        self.assertTrue(result['signedIn'])

    def test_native_denial_is_safe_and_retryable(self):
        result = self.exercise('denied')
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(result['error'], 'Dummy authorization denied')

    def test_native_cancel_stops_the_owned_child(self):
        result = self.exercise('cancel')
        self.assertEqual(result['phase'], 'idle')
        self.assertEqual(result['error'], '')

    def test_native_failed_start_does_not_stay_busy(self):
        result = self.exercise('failed-start')
        self.assertEqual(result['phase'], 'failed')
        self.assertTrue(result['error'])


if __name__ == '__main__':
    unittest.main()
