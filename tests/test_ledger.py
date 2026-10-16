"""Synthetic tests: never inspect real Codex records or install a real job."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import app


def usage(n):
    return dict(input_tokens=n, cached_input_tokens=n // 2, output_tokens=n // 10,
                reasoning_output_tokens=n // 20, total_tokens=n + n // 10)


def event(n, last=None, stamp='2026-10-01T15:00:00Z'):
    return {'type': 'event_msg', 'timestamp': stamp, 'payload': {'type': 'token_count',
            'info': {'total_token_usage': usage(n), 'last_token_usage': usage(n if last is None else last)}}}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        self.data = self.root / 'data'
        self.source = self.home / 'sessions' / '2026' / '10' / '01'
        self.source.mkdir(parents=True)
        self.sid = 'a' * 36
        self.path = self.source / ('rollout-' + self.sid + '.jsonl')
        with sqlite3.connect(self.home / 'state_5.sqlite') as db:
            db.executescript('''CREATE TABLE projects (id TEXT, name TEXT);
            CREATE TABLE project_roots (project_id TEXT,path TEXT);
            CREATE TABLE threads (id TEXT,rollout_path TEXT,cwd TEXT,project_id TEXT,model_provider TEXT);
            INSERT INTO projects VALUES ('project-one','合成项目');
            INSERT INTO project_roots VALUES ('project-one','/synthetic/project');''')
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?)', (self.sid, str(self.path), '/synthetic/project', 'project-one', 'openai'))

    def tearDown(self):
        self.temp.cleanup()

    def write(self, events, mode='w', path=None):
        with (path or self.path).open(mode) as stream:
            for e in events:
                stream.write(json.dumps(e) + '\n')

    def seed(self):
        return [{'type': 'session_meta', 'payload': {'cwd': '/synthetic/project', 'model_provider': 'openai', 'base_instructions': 'PRIVATE_SYNTHETIC_TEXT'}},
                {'type': 'turn_context', 'payload': {'model': 'test-model'}}]

    def report(self):
        return app.report(self.data, {'timezone': 'Asia/Shanghai'})

    def total(self):
        return sum(r['total_tokens'] for r in self.report()['rows'])

    def test_incremental_duplicate_and_model_change(self):
        self.write(self.seed() + [event(100), event(100), event(200,100)])
        first = app.collect(self.home,self.data)
        self.assertEqual(first['new_events'],2)
        self.assertEqual(self.total(),220)
        second = app.collect(self.home,self.data)
        self.assertEqual(second['bytes_read'],0)
        self.assertEqual(second['new_events'],0)
        self.write([{'type':'turn_context','payload':{'model':'other-model'}},event(300,100,stamp='2026-10-01T16:01:00Z')], 'a')
        app.collect(self.home,self.data)
        report=self.report()
        self.assertEqual(self.total(),330)
        self.assertEqual({r['day'] for r in report['rows']},{'2026-10-01','2026-10-02'})
        self.assertEqual({r['model'] for r in report['rows']},{'test-model','other-model'})
        self.assertNotIn(b'PRIVATE_SYNTHETIC_TEXT',(self.data/'usage.sqlite').read_bytes())

    def test_partial_line_retried(self):
        self.write(self.seed()+[event(100)])
        raw=json.dumps(event(200,100))
        with self.path.open('a') as f:f.write(raw[:40])
        app.collect(self.home,self.data)
        self.assertEqual(self.total(),110)
        with self.path.open('a') as f:f.write(raw[40:]+'\n')
        app.collect(self.home,self.data)
        self.assertEqual(self.total(),220)

    def test_archived_move_and_truncate_do_not_duplicate(self):
        values=self.seed()+[event(100),event(200,100)]
        self.write(values);app.collect(self.home,self.data)
        folder=self.home/'archived_sessions';folder.mkdir()
        archived=folder/self.path.name;self.path.rename(archived)
        app.collect(self.home,self.data)
        self.assertEqual(self.total(),220)
        self.write(values, path=archived);app.collect(self.home,self.data)
        self.assertEqual(self.total(),220)

    def test_copied_fork_and_inherited_baseline(self):
        self.write(self.seed()+[event(100)])
        child=self.source/('rollout-'+'b'*36+'.jsonl')
        self.write(self.seed()+[event(100),event(200,100,stamp='2026-10-01T16:00:00Z')],path=child)
        app.collect(self.home,self.data)
        self.assertEqual(self.total(),220)
        self.assertEqual(len({r['project'] for r in self.report()['rows']}),1)
        baseline=self.source/('rollout-'+'c'*36+'.jsonl')
        self.write(self.seed()+[event(1000,50,stamp='2026-10-02T16:00:00Z')],path=baseline)
        result=app.collect(self.home,self.data)
        self.assertEqual(self.total(),275)
        self.assertEqual(result['inherited_baselines'],1)

    def test_reset_and_malformed_record(self):
        self.write(self.seed()+[event(100),event(30,30,stamp='2026-10-02T12:00:00Z')])
        with self.path.open('a') as f:f.write('invalid-json\n')
        result=app.collect(self.home,self.data)
        self.assertEqual(self.total(),143)
        self.assertEqual(result['counter_resets'],1)
        self.assertEqual(len(result['warnings']),1)
        self.assertNotIn('invalid-json',str(result))

    def test_quota_without_usage_and_unknown_model(self):
        snapshot={'type':'event_msg','timestamp':'2026-10-02T12:00:00Z','payload':{'type':'token_count','info':None,'rate_limits':{'primary':{'used_percent':7}}}}
        self.write([event(100),snapshot]);app.collect(self.home,self.data)
        self.assertEqual(self.report()['quota']['limits']['primary']['used_percent'],7)
        self.assertEqual(self.report()['rows'][0]['model'],'unknown')

    def test_failure_preserves_previous_statistics(self):
        self.write(self.seed()+[event(100)]);app.collect(self.home,self.data)
        self.path.unlink()
        with self.assertRaises(RuntimeError):app.collect(self.home,self.data)
        self.assertEqual(self.total(),110)

    def test_worktree_attribution(self):
        main=self.root/'main';git=main/'.git';work=self.root/'work';work.mkdir();gd=git/'worktrees'/'branch';gd.mkdir(parents=True)
        (gd/'commondir').write_text('../..')
        (work/'.git').write_text('gitdir: '+str(gd))
        self.assertEqual(app.original_worktree(str(work)),str(main.resolve()))


if __name__ == '__main__':
    unittest.main()
