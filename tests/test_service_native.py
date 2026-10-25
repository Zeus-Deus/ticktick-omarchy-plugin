#!/usr/bin/env python3
"""Opt-in native Service/Process stdin tests; run in the Omarchy VM.

TICKTICK_NATIVE_TESTS=1 python3 -m unittest discover -s tests -p test_service_native.py
No plugin installation, live shell mutation, credentials, or network access.
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


@unittest.skipUnless(os.environ.get("TICKTICK_NATIVE_TESTS") == "1", "opt-in Omarchy VM native tests")
class NativeHandoffTests(unittest.TestCase):
    def exercise(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            rig = Path(tmp)
            home = rig / "home"
            runtime = rig / "runtime"
            home.mkdir(mode=0o700)
            runtime.mkdir(mode=0o700)
            for filename in ("Service.qml", "Model.js"):
                shutil.copyfile(ROOT / filename, rig / filename)
            if mode == "queued":
                # Expose the real private Process in this test copy only.
                service = (rig / "Service.qml").read_text()
                (rig / "Service.qml").write_text(service.replace(
                    "  id: root", "  id: root\n  property alias testActionProc: actionProc", 1))
            (rig / "bin").mkdir()
            wrapper = rig / "bin" / "omarchy-ticktick"
            if mode != "missing":
                wrapper.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(f"""\
                    import json, os, runpy, stat, sys
                    from pathlib import Path
                    cli = runpy.run_path({str(ROOT / 'bin' / 'omarchy-ticktick')!r})['main'].__globals__
                    receipt = {{'argv': sys.argv[1:], 'validated': False, 'privateWrites': []}}
                    original_dump = json.dump
                    def observed_dump(payload, handle, **kwargs):
                        info = os.fstat(handle.fileno())
                        receipt['privateWrites'].append([stat.S_IMODE(info.st_mode), info.st_size])
                        return original_dump(payload, handle, **kwargs)
                    json.dump = observed_dump
                    def fetch(session):
                        assert session['token'] == 'dummy-native-cookie'
                        receipt['validated'] = True
                        if {mode!r} == 'reject':
                            raise cli['TickTickError']('Synthetic login rejection')
                        return {{'tasks': [], 'habits': []}}
                    cli['fetch_state'] = fetch
                    # No request can escape this fixture, including startup syncs.
                    cli['request'] = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Unexpected network access'))
                    code = cli['main'](sys.argv[1:])
                    receipt['exit'] = code
                    Path({str(rig / 'receipt.json')!r}).write_text(json.dumps(receipt))
                    sys.exit(code)
                """))
                wrapper.chmod(0o700)
            (rig / "shell.qml").write_text(textwrap.dedent("""\
                import QtQuick
                import Quickshell
                ShellRoot {
                  property bool submitted: false
                  Service {
                    id: svc
                    settings: ({ syncInterval: "Only when opened" })
                  }
                  Component.onCompleted: Qt.callLater(function() {
                    PRECEDING_ACTION
                    svc.connectWithToken("dummy-native-cookie")
                    submitted = true
                  })
                  Timer {
                    interval: 25; running: true; repeat: true
                    onTriggered: {
                      if (submitted && !svc.connecting) {
                        console.log("HANDOFF_RESULT " + JSON.stringify({ cleared: svc.loginToken === "", connecting: svc.connecting }))
                        Qt.quit()
                      }
                    }
                  }
                  Timer {
                    interval: 5000; running: true
                    onTriggered: { console.log("HANDOFF_TIMEOUT"); Qt.quit() }
                  }
                }
            """).replace("PRECEDING_ACTION", (
                'svc.testActionProc.command = ["/nonexistent-ticktick-test-command"]; '
                'svc.testActionProc.running = true;'
            ) if mode == "queued" else ""))
            # Deliberately do not inherit the user's HOME, XDG paths or display.
            env = {
                "PATH": os.environ["PATH"], "HOME": str(home),
                "XDG_RUNTIME_DIR": str(runtime), "QT_QPA_PLATFORM": "offscreen",
                "TMPDIR": str(rig), "LANG": "C.UTF-8",
            }
            result = subprocess.run(["qs", "-p", str(rig / "shell.qml")],
                                    env=env, capture_output=True, text=True, timeout=12)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn("HANDOFF_TIMEOUT", output)
            self.assertIn('HANDOFF_RESULT {"cleared":true,"connecting":false}', output)
            self.assertNotIn("dummy-native-cookie", output)
            state = home / ".local/state/omarchy/ticktick"
            self.assertFalse((state / "token-paste").exists())
            if mode == "missing":
                self.assertFalse((rig / "receipt.json").exists())
                self.assertFalse((state / "session.json").exists())
                return
            self.assertTrue((rig / "receipt.json").exists(), output)
            receipt = json.loads((rig / "receipt.json").read_text())
            self.assertEqual(receipt["argv"], ["login", "--token-stdin"])
            self.assertTrue(receipt["validated"])
            succeeds = mode in ("success", "queued")
            self.assertEqual(receipt["exit"], 0 if succeeds else 1)
            if succeeds:
                self.assertEqual(receipt["privateWrites"], [[0o600, 0], [0o600, 0]])
                self.assertEqual(stat_mode(state), 0o700)
                self.assertEqual(stat_mode(state / "session.json"), 0o600)
            else:
                self.assertFalse((state / "session.json").exists())
                self.assertEqual(receipt["privateWrites"], [])

    def test_successful_native_stdin_login(self):
        self.exercise("success")

    def test_rejected_native_login_cleans_up(self):
        self.exercise("reject")

    def test_failed_preceding_process_does_not_strand_login(self):
        self.exercise("queued")

    def test_failed_process_start_clears_pending_token(self):
        self.exercise("missing")


def stat_mode(path):
    import stat
    return stat.S_IMODE(path.stat().st_mode)


if __name__ == "__main__":
    unittest.main()
