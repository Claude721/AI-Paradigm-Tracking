"""Execute artifact-selection shell with a local gh stub, no GitHub access."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecoveryWorkflowTests(unittest.TestCase):
    def workflow(self):
        return (ROOT/'.github/workflows/recover-delivery.yml').read_text()

    def test_state_only_authority_and_backup_before_mutation(self):
        workflow = self.workflow()
        self.assertIn('permissions:\n  contents: read\n  actions: read',workflow)
        self.assertNotIn('actions: write',workflow)
        self.assertIn('on:\n  workflow_dispatch:',workflow)
        self.assertNotIn('schedule:',workflow)
        self.assertIn('group: paradigm-radar-delivery',workflow)
        text = workflow
        self.assertNotIn('secrets.',text)
        self.assertNotIn('workflow run',text)
        self.assertNotIn('python main.py',text)
        backup=text.index('name: delivery-recovery-backup-')
        apply=text.index('--apply')
        upload=text.index('name: paradigm-radar-v0-state-')
        self.assertLess(backup,apply)
        self.assertLess(apply,upload)
        state_upload=text[text.rfind('      - name:',0,upload):text.find('\n      - name:',upload)]
        self.assertNotIn('always()',state_upload)

    def selection(self, *, requested='123', newest='123', digest=None):
        data=b'approved-state-archive-bytes'
        actual=hashlib.sha256(data).hexdigest()
        # Extract our literal shell block; no YAML dependency is needed in the
        # production/test environment. The actual shell, not a parser mock,
        # executes below against the local gh fixture.
        blocks=re.findall(r'(?m)^        run: \|\n((?:(?: {10}[^\n]*|)\n)+)',self.workflow())
        script=textwrap.dedent(next(value for value in blocks if 'SOURCE_RUN_ID' in value))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); tools=root/'bin'; tools.mkdir()
            stub=tools/'gh'
            stub.write_text('#!'+sys.executable+'\n'+'''import os,sys,base64
args=sys.argv[1:]
if '--paginate' in args:
 print(os.environ['FAKE_ROWS'])
elif args[-1].endswith('/zip'):
 sys.stdout.buffer.write(base64.b64decode(os.environ['FAKE_ZIP']))
elif '/actions/runs/' in args[1]:
 print('a'*40)
else:
 raise SystemExit('unexpected gh API')
''')
            stub.chmod(0o700)
            environment=os.environ.copy()
            environment.update(PATH=str(tools)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+environment.get('PATH',''),
                SOURCE_RUN_ID=requested,GITHUB_REPOSITORY='fixture/repository',GH_TOKEN='fixture-read-only',
                GITHUB_ENV=str(root/'github.env'),FAKE_ZIP=base64.b64encode(data).decode(),
                FAKE_ROWS=f'9\tparadigm-radar-v0-state-{newest}\t2026-11-03T00:00:00Z\t{digest if digest is not None else "sha256:"+actual}\n'
                          '8\tparadigm-radar-v0-state-122\t2026-11-02T00:00:00Z\tsha256:older')
            result=subprocess.run(['bash','-c',script],cwd=root,env=environment,capture_output=True,text=True,timeout=15)
            archive=root/'.private/recovery-before/source-state.zip'
            envfile=root/'github.env'
            return result, archive.read_bytes() if archive.exists() else None, envfile.read_text() if envfile.exists() else ''

    def test_download_uses_selected_id_without_reading_deferred_github_env(self):
        result,data,environment=self.selection()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(data,b'approved-state-archive-bytes')
        self.assertIn('RECOVERY_ARTIFACT_ID=9',environment)
        self.assertIn('RECOVERY_SOURCE_DIGEST=',environment)

    def test_newer_state_stops_before_downloading_or_mutating(self):
        result,data,_=self.selection(newest='124')
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(data)
        self.assertIn('已有更新状态',result.stdout+result.stderr)

    def test_digest_mismatch_cannot_reach_repair(self):
        result,_,_=self.selection(digest='sha256:'+'0'*64)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('下载摘要不一致',result.stdout+result.stderr)

    def test_shell_metacharacters_in_run_id_are_not_executed(self):
        result,data,_=self.selection(requested='123; exit 0')
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(data)
        self.assertIn('运行ID必须为数字',result.stdout+result.stderr)
