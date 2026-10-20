import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from projections.project_sources import Reader, card_roles, execution_observation, observe_projects, discover_cards


class ProjectSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def file(self, name, text):
        p = self.root / name
        p.write_text(text)
        return p

    def binding(self, extra=None):
        data = {'taskId':'task-one', 'attemptId':'att-one', 'agentId':'dev-ckb1', 'sourceId':'codebuddy-cli', 'db':str(self.root/'ledger.sqlite'), 'phase':'TEST', 'status':'awaiting_review'}
        data.update(extra or {})
        path = self.file('workflow.json', json.dumps(data))
        return {'path':str(path), 'root':str(self.root)}

    def db(self):
        c = sqlite3.connect(self.root/'ledger.sqlite')
        c.executescript('''
          CREATE TABLE tasks(task_id,agent_id,source_id,status,current_attempt_id,workspace);
          CREATE TABLE attempts(attempt_id,task_id,state);
          CREATE TABLE deliveries(delivery_id,task_id,attempt_id,report_path,created_at);
          CREATE TABLE reviews(review_id,task_id,delivery_id,reviewer_id,decision,created_at);
          INSERT INTO tasks VALUES('task-one','dev-ckb1','codebuddy-cli','awaiting_review','att-one','workspace');
          INSERT INTO attempts VALUES('att-one','task-one','delivered');
          INSERT INTO deliveries VALUES('del-one','task-one','att-one','report.md',2);
          INSERT INTO deliveries VALUES('del-old','task-one','att-old','old.md',1);
          INSERT INTO reviews VALUES('rev-old','task-one','del-old','qa-wkb1','approved',1);
          INSERT INTO reviews VALUES('rev-one','task-one','del-one','cox','request_info',2);
        ''')
        c.commit();c.close()

    def test_current_attempt_only_and_roles_separate_no_ledger_write(self):
        self.db();before=(self.root/'ledger.sqlite').read_bytes()
        x=execution_observation(self.binding(), Reader())
        self.assertTrue(x['ledgerMatched']);self.assertEqual(x['developer'],'dev-ckb1')
        self.assertEqual([r['reviewer_id'] for r in x['reviews']],['cox'])
        self.assertIn('未找到独立 QA',x['qa']);self.assertEqual(x['runtimeIdentity'],'UNVERIFIED')
        self.assertEqual((self.root/'ledger.sqlite').read_bytes(),before)

    def test_identity_mismatch_does_not_claim_developer_or_pass(self):
        self.db();x=execution_observation(self.binding({'agentId':'dev-wkb1'}),Reader())
        self.assertFalse(x['available']);self.assertNotIn('developer',x);self.assertNotIn('reviews',x)

    def test_prepared_round_does_not_reuse_previous_review(self):
        self.db();x=execution_observation(self.binding({'taskId':None,'attemptId':None}),Reader())
        self.assertTrue(x['available']);self.assertNotIn('developer',x);self.assertNotIn('reviews',x)

    def test_dynamic_db_cannot_escape_binding_root(self):
        outside=Path(self.temp.name).parent/'outside.sqlite'
        x=execution_observation(self.binding({'db':str(outside)}),Reader())
        self.assertFalse(x['available']);self.assertNotIn('developer',x)

    def test_symlink_source_rejected(self):
        original=self.file('original.json','{}');link=self.root/'link.json';link.symlink_to(original)
        with self.assertRaises(ValueError):Reader().read(link,self.root)

    def test_markdown_pass_never_becomes_execution_status(self):
        p=self.file('plan.md','# Result\nPASS all complete\n')
        rows=[{'projectId':'p','tasks':[{'status':'partial','member':'项目原会话','watchedSources':[{'path':str(p),'root':str(self.root),'sha256':'0'*64}]}]}]
        x=observe_projects(rows)[0]['tasks'][0]
        self.assertEqual(x['status'],'partial');self.assertIn('已变化',x['sourceUpdateNote']);self.assertNotIn('executionFacts',x)

    def test_roles_header_not_filename_or_body(self):
        self.assertEqual(card_roles(b'# dev-ckb1 PASS qa-wkb1\n'),{})
        text='---\nexecutor:\n  agentId: dev-ckb1\nreviewer:\n  alias: cox\n---\n# qa-wkb1 completed\n'
        self.assertEqual(card_roles(text.encode()),{'executor':'dev-ckb1','reviewer':'cox'})

    def test_oversized_source_fails_closed(self):
        p=self.file('large.md','x'*(513*1024))
        with self.assertRaises(ValueError):Reader().read(p,self.root)

    def test_missing_source_preserves_snapshot_and_is_not_verified(self):
        rows=[{'projectId':'p','tasks':[{'status':'planned','member':'unknown','watchedSources':[{'path':str(self.root/'missing.md'),'root':str(self.root)}]}]}]
        x=observe_projects(rows)[0]['tasks'][0]
        self.assertFalse(x['sourceObservations'][0]['available']);self.assertEqual(x['status'],'planned')

    def test_new_card_discovered_without_reading_symlink(self):
        self.file('one.md', '---\nexecutor:\n  agentId: dev-wkb1\n---\n# one\n')
        (self.root/'secret-link.md').symlink_to(self.root/'one.md')
        cards,note=discover_cards({'projectId':'p','cardRoots':[str(self.root)],'cardSources':[]})
        self.assertIsNone(note);self.assertEqual([Path(c['path']).name for c in cards],['one.md'])

    def test_current_state_changes_on_next_read_without_snapshot_writes(self):
        self.db();binding=self.binding();before=(self.root/'workflow.json').read_bytes()
        self.assertEqual(execution_observation(binding,Reader())['ledgerState'],'awaiting_review')
        c=sqlite3.connect(self.root/'ledger.sqlite');c.execute("UPDATE tasks SET status='changes_requested'");c.commit();c.close()
        self.assertEqual(execution_observation(binding,Reader())['ledgerState'],'changes_requested')
        self.assertEqual((self.root/'workflow.json').read_bytes(),before)

    def test_legacy_binding_can_read_explicit_task_identity_without_overriding_new_stage(self):
        self.db();binding=self.binding({'agentId':None,'sourceId':None})
        binding.update(historical=True,scopeNote='old candidate')
        rows=[{'projectId':'new','stage':'rc.2','tasks':[{'status':'blocked','member':'unknown','executionSource':binding}]}]
        p=observe_projects(rows)[0];self.assertEqual(p['stage'],'rc.2');self.assertNotIn('currentExecutionStages',p)
        self.assertEqual(p['tasks'][0]['status'],'blocked');self.assertEqual(p['tasks'][0]['executionFacts']['developer'],'dev-ckb1')

    def test_qa_executor_role_is_explicit_and_not_inferred_from_agent_name(self):
        self.assertEqual(card_roles(b'---\nexecutor:\n  agentId: qa-wkb1\n  role: qa-reporter\n---\n')['qa'],'qa-wkb1')
        self.assertNotIn('qa',card_roles(b'---\nexecutor:\n  agentId: qa-wkb1\n---\n'))


if __name__ == '__main__':
    unittest.main()
