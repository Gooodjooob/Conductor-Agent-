import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from orchestrator.gateway import Rejected
from orchestrator.scheduler import Scheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.current = {}
        self.calls = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.scheduler = Scheduler(str(self.root/'state'), self.read, self.launch, 2)
        self.plan = {'planId':'p1','title':'集成开发','revision':1,'maxConcurrency':2,
                     'members':[{'memberId':'dev1','host':'dsh','roles':['developer'],'maxConcurrency':1},
                                {'memberId':'dev2','host':'claude','roles':['developer'],'maxConcurrency':1}],
                     'nodes':[self.node('a','dev1'),self.node('b','dev2'),self.node('c','dev1',['a','b'])]}
        for task, member in [('a','dev1'),('b','dev2'),('c','dev1')]: self.add_task(task, member)

    def add_task(self, task, member):
        workspace = self.root/task; workspace.mkdir()
        self.current[task] = {'taskId':task, 'state':'todo', 'attemptId':None, 'verdicts':{},
            'contract':{'taskId':task,'workerId':member,'workspace':str(workspace),'revision':1,'issueId':'issue-'+task}}

    def node(self, task, member, deps=None):
        return {'taskId':task,'title':task,'memberId':member,'role':'developer','dependsOn':deps or []}

    def read(self, task): return copy.deepcopy(self.current[task])

    def launch(self, node, member, attempt):
        self.calls.append(node['taskId'])
        self.current[node['taskId']].update(state='unknown',attemptId=attempt)
        self.started.set()
        self.release.wait(5)

    def tearDown(self):
        self.release.set(); self.scheduler.close(); self.tmp.cleanup()

    def rejected(self, code, fn):
        with self.assertRaises(Rejected) as ctx: fn()
        self.assertEqual(ctx.exception.code, code)

    def finish(self, task, done=False):
        self.current[task].update(state='done' if done else 'awaiting_validation', syncState='synced' if done else 'not_synced',
                                  verdicts={'qa':'pass','reviewer':'pass','controller':'pass'} if done else {})

    def test_parallel_roots_wait_for_synced_dependencies(self):
        self.scheduler.submit_plan(self.plan); self.scheduler.tick(); self.started.wait(1)
        self.scheduler.tick()
        self.assertEqual(set(self.calls), {'a','b'})
        self.release.set()
        self.finish('a'); self.finish('b')
        self.scheduler.tick(); self.assertNotIn('c',self.calls)
        self.finish('a',True); self.finish('b',True)
        self.scheduler.tick()
        self.scheduler.close()
        self.assertEqual(self.calls.count('c'),1)

    def test_restart_does_not_replay_uncertain_dispatch(self):
        self.scheduler.submit_plan(self.plan); self.scheduler.tick(); self.started.wait(1)
        self.release.set(); self.scheduler.close()
        second = Scheduler(str(self.root/'state'),self.read,self.launch,2)
        try:
            second.tick(); second.tick()
            self.assertEqual(sorted(self.calls),['a','b'])
            self.assertEqual([c['state'] for c in second.snapshot()['cards']][:2],['unknown','unknown'])
        finally: second.close()

    def test_gateway_outage_blocks_new_launch(self):
        self.scheduler.submit_plan(self.plan)
        self.scheduler.read_task=lambda task: (_ for _ in ()).throw(OSError())
        self.scheduler.tick()
        self.assertEqual(self.calls,[])

    def test_cancel_queued_descendants_and_late_result_do_not_unlock(self):
        self.scheduler.submit_plan(self.plan); self.scheduler.tick(); self.started.wait(1)
        self.assertEqual(self.scheduler.cancel('a'),['a','c'])
        self.release.set(); self.scheduler.close()
        self.finish('a',True); self.finish('b',True)
        snap=self.scheduler.tick()
        self.assertNotIn('c',self.calls)
        self.assertTrue(next(c for c in snap['cards'] if c['taskId']=='a')['cancelRequested'])
        self.assertFalse(next(c for c in snap['cards'] if c['taskId']=='a')['stopConfirmed'])

    def test_duplicate_plan_is_idempotent_after_dispatch(self):
        self.scheduler.submit_plan(self.plan); self.scheduler.tick(); self.started.wait(1)
        self.scheduler.submit_plan(copy.deepcopy(self.plan))
        changed=copy.deepcopy(self.plan); changed['revision']=2
        self.rejected('IMMUTABLE_PLAN',lambda:self.scheduler.submit_plan(changed))

    def test_cycle_missing_dependency_and_duplicate_node(self):
        bad=copy.deepcopy(self.plan); bad['nodes'][0]['dependsOn']=['c']
        self.rejected('DEPENDENCY_CYCLE',lambda:self.scheduler.submit_plan(bad))
        bad=copy.deepcopy(self.plan); bad['nodes'][0]['dependsOn']=['missing']
        self.rejected('MISSING_DEPENDENCY',lambda:self.scheduler.submit_plan(bad))
        bad=copy.deepcopy(self.plan); bad['nodes'].append(bad['nodes'][0])
        self.rejected('DUPLICATE_OR_INVALID_NODE',lambda:self.scheduler.submit_plan(bad))

    def test_workspace_overlap_and_capability_mismatch(self):
        self.current['b']['contract']['workspace']=self.current['a']['contract']['workspace']
        self.rejected('WORKSPACE_NOT_INDEPENDENT',lambda:self.scheduler.submit_plan(self.plan))
        self.current['b']['contract']['workspace']=str(self.root/'b')
        bad=copy.deepcopy(self.plan); bad['nodes'][0]['role']='reviewer'
        self.rejected('MEMBER_CAPABILITY_MISMATCH',lambda:self.scheduler.submit_plan(bad))

    def test_boolean_revision_rejected(self):
        self.plan['revision']=True
        self.rejected('INVALID_PLAN',lambda:self.scheduler.submit_plan(self.plan))

    def test_unknown_reserves_capacity(self):
        self.scheduler.limit=1
        self.scheduler.submit_plan(self.plan); self.scheduler.tick(); self.started.wait(1)
        self.release.set(); self.scheduler.close(); self.scheduler.tick()
        self.assertEqual(self.calls,['a'])

    def test_contract_drift_blocks_dispatch(self):
        self.scheduler.submit_plan(self.plan)
        self.current['a']['contract']['revision']=2
        snap=self.scheduler.tick()
        self.assertNotIn('a',self.calls)
        self.assertEqual(next(c for c in snap['cards'] if c['taskId']=='a')['error'],'SCHEDULER_CONTRACT_DRIFT')

    def test_crash_after_intent_before_launch_does_not_replay(self):
        self.scheduler.submit_plan(self.plan)
        self.scheduler.pool.shutdown()
        self.scheduler.tick(); self.scheduler.tick()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.scheduler.snapshot()['cards'][0]['state'],'unknown')

    def test_supersede_uses_new_ids_workspaces_and_stops_old(self):
        self.scheduler.submit_plan(self.plan)
        new=copy.deepcopy(self.plan); new.update(planId='p2',revision=2,nodes=[self.node('d','dev1')])
        self.add_task('d','dev1'); self.scheduler.supersede('p1',new)
        self.scheduler.tick(); self.started.wait(1)
        self.assertEqual(self.calls,['d'])
        self.assertEqual(next(p for p in self.scheduler.snapshot()['plans'] if p['planId']=='p1')['status'],'superseded')

    def test_supersede_rejects_reused_workspace(self):
        self.scheduler.submit_plan(self.plan)
        new=copy.deepcopy(self.plan); new.update(planId='p2',revision=2,nodes=[self.node('d','dev1')])
        self.add_task('d','dev1'); self.current['d']['contract']['workspace']=self.current['a']['contract']['workspace']
        self.rejected('REVISION_WORKSPACE_OVERLAP',lambda:self.scheduler.supersede('p1',new))


if __name__=='__main__': unittest.main()
