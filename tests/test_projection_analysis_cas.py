from datetime import datetime,timezone
from types import SimpleNamespace
import os
import time
import threading
import unittest
from unittest.mock import patch
from redis.exceptions import RedisError
import test_projection as fixtures
import test_projection_resources as resources
from app.projection_layout import publish_analysis,refresh_analysis_failure,submit_analysis,projection_queue
from app.projection_store import ProjectionRejected

class AnalysisCASTests(unittest.TestCase):
    setUp=fixtures.ProjectionTests.setUp
    identity=fixtures.ProjectionTests.identity
    upload=fixtures.ProjectionTests.upload
    tile=fixtures.ProjectionTests.tile
    metadata=fixtures.ProjectionTests.metadata
    renew=fixtures.ProjectionTests.renew
    create=resources.ResourceTests.create
    policy_restore=resources.ResourceTests.policy_restore
    queue_environment=fixtures.ProjectionTests.queue_environment

    def test_pre_read_different_modes_replace_queued_job_and_lease(self):
        document=self.create()
        first_snapshot=self.ledger.load(document,self.owner)
        second_snapshot=self.ledger.load(document,self.owner)
        environment,redis=self.queue_environment()
        with environment,self.app.app_context():
            first=submit_analysis(self.ledger,document,self.owner,first_snapshot,'current')
            first_lease=self.ledger.load(document,self.owner)['analysis']['lease']
            second=submit_analysis(self.ledger,document,self.owner,second_snapshot,'original')
            self.assertNotEqual(first['analysis_id'],second['analysis_id'])
            self.assertEqual(projection_queue(redis).job_ids,[second['analysis_id']])
            self.assertIsNone(self.ledger.lease(first_lease))

    def test_pre_read_same_mode_is_idempotent(self):
        document=self.create()
        snapshots=[self.ledger.load(document,self.owner) for _ in range(2)]
        environment,redis=self.queue_environment()
        with environment,self.app.app_context():
            first=submit_analysis(self.ledger,document,self.owner,snapshots[0],'current')
            second=submit_analysis(self.ledger,document,self.owner,snapshots[1],'current')
            self.assertEqual(first['analysis_id'],second['analysis_id'])
            self.assertEqual(projection_queue(redis).job_ids,[first['analysis_id']])

    def test_pre_read_mode_switch_revokes_running_analysis_but_keeps_tile(self):
        document=self.create()
        snapshots=[self.ledger.load(document,self.owner) for _ in range(2)]
        environment,redis=self.queue_environment()
        running=None
        try:
            with environment,self.app.app_context():
                first=submit_analysis(self.ledger,document,self.owner,snapshots[0],'current')
                running=self.ledger.load(document,self.owner)['analysis']['lease']
                # A live supervisor keeps its reservation until it reaps its child.
                self.ledger.attach(running,os.getpid())
                tile=self.ledger.reserve(self.owner,'tile',document,0)
                second=submit_analysis(self.ledger,document,self.owner,snapshots[1],'original')
                self.assertFalse(self.ledger.valid_lease(running))
                self.assertIsNotNone(self.ledger.lease(running))
                self.assertTrue(self.ledger.valid_lease(tile))
                self.assertEqual(self.ledger.load(document,self.owner)['_generation'],0)
                self.assertEqual(projection_queue(redis).job_ids,[second['analysis_id']])
                self.ledger.release(tile)
        finally:
            if running:
                with self.ledger.transaction() as db:
                    db.execute('UPDATE leases SET pid=NULL,identity=NULL WHERE id=?',(running,))
                self.ledger.release(running)

    def test_replacement_cas_loser_does_not_cancel_winning_analysis(self):
        document=self.create()
        stale=self.ledger.load(document,self.owner)
        environment,redis=self.queue_environment()
        replace=self.ledger.replace_analysis
        winner=[]
        with environment,self.app.app_context():
            first=submit_analysis(self.ledger,document,self.owner,dict(stale),'current')
            def concurrent_winner(document,owner,analysis,generation,expected):
                lease=self.ledger.reserve(owner,'analysis',document,generation)
                current={'analysis_id':'concurrent-winner','status':'analyzing',
                         'mode':'original','phase':'queued','lease':lease}
                self.assertTrue(replace(document,owner,current,generation,expected))
                winner.append(lease)
                # Execute the losing transaction's real identity guard.
                return replace(document,owner,analysis,generation,expected)
            with patch.object(self.ledger,'replace_analysis',side_effect=concurrent_winner), \
                 patch('app.projection_layout.discard_analysis_job') as discard:
                with self.assertRaises(ProjectionRejected) as rejected:
                    submit_analysis(self.ledger,document,self.owner,stale,'original')
                self.assertEqual(rejected.exception.status,409)
                discard.assert_not_called()
            self.assertEqual(self.metadata(document)['analysis']['analysis_id'],'concurrent-winner')
            self.assertTrue(self.ledger.valid_lease(winner[0]))
            self.assertEqual(projection_queue(redis).job_ids,[first['analysis_id']])
            with self.ledger.transaction() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM leases WHERE kind='analysis'").fetchone()[0],2)

    def test_replacement_write_failure_rolls_back_old_lease_and_metadata(self):
        document=self.create()
        stale=self.ledger.load(document,self.owner)
        environment,redis=self.queue_environment()
        with environment,self.app.app_context():
            first=submit_analysis(self.ledger,document,self.owner,dict(stale),'current')
            before=self.ledger.load(document,self.owner)
            lease=before['analysis']['lease']
            tile=self.ledger.reserve(self.owner,'tile',document,0)
            with patch.object(self.ledger,'_write_metadata',side_effect=OSError('controlled disk failure')), \
                 patch('app.projection_layout.discard_analysis_job') as discard:
                with self.assertRaises(OSError):
                    submit_analysis(self.ledger,document,self.owner,stale,'original')
                discard.assert_not_called()
            self.assertEqual(self.ledger.load(document,self.owner),before)
            self.assertTrue(self.ledger.valid_lease(lease))
            self.assertTrue(self.ledger.valid_lease(tile))
            self.assertEqual(projection_queue(redis).job_ids,[first['analysis_id']])
            with self.ledger.transaction() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM leases WHERE kind='analysis'").fetchone()[0],1)
            self.ledger.release(tile)

    def test_committed_replacement_revokes_old_lease_before_cleanup_can_abort(self):
        document=self.create()
        stale=self.ledger.load(document,self.owner)
        environment,redis=self.queue_environment()
        with environment,self.app.app_context():
            first=submit_analysis(self.ledger,document,self.owner,dict(stale),'current')
            old_lease=self.metadata(document)['analysis']['lease']
            tile=self.ledger.reserve(self.owner,'tile',document,0)
            # Model a supervisor exit immediately after commit, before Redis or
            # filesystem cleanup. The SQLite lease invalidation must be durable.
            with patch('app.projection_layout.discard_analysis_job',side_effect=SystemExit('controlled supervisor exit')):
                with self.assertRaises(SystemExit):
                    submit_analysis(self.ledger,document,self.owner,stale,'original')
            self.assertFalse(self.ledger.valid_lease(old_lease))
            latest=self.ledger.load(document,self.owner)
            self.assertNotEqual(latest['analysis']['analysis_id'],first['analysis_id'])
            self.assertTrue(self.ledger.valid_lease(latest['analysis']['lease']))
            self.assertTrue(self.ledger.valid_lease(tile))
            self.assertEqual(latest['_generation'],0)
            self.ledger.release(tile)

    def test_replacement_keeps_metadata_published_between_read_and_cas(self):
        document=self.create()
        stale=self.ledger.load(document,self.owner)
        environment,redis=self.queue_environment()
        replace=self.ledger.replace_analysis
        with environment,self.app.app_context():
            submit_analysis(self.ledger,document,self.owner,dict(stale),'current')
            def publish_before_replace(document,owner,analysis,generation,expected):
                latest=self.ledger.load(document,owner)
                latest['concurrent_metadata']='preserved'
                self.assertTrue(self.ledger.update(document,owner,latest,generation))
                return replace(document,owner,analysis,generation,expected)
            with patch.object(self.ledger,'replace_analysis',side_effect=publish_before_replace):
                second=submit_analysis(self.ledger,document,self.owner,stale,'original')
            latest=self.ledger.load(document,self.owner)
            self.assertEqual(latest['concurrent_metadata'],'preserved')
            self.assertEqual(latest['analysis']['analysis_id'],second['analysis_id'])
            self.assertEqual(projection_queue(redis).job_ids,[second['analysis_id']])

    def test_mode_replacement_reuses_existing_slot_in_full_queue(self):
        document=self.create()
        snapshots=[self.ledger.load(document,self.owner) for _ in range(2)]
        environment,redis=self.queue_environment()
        with environment,self.app.app_context():
            first=submit_analysis(self.ledger,document,self.owner,snapshots[0],'current')
            queue=projection_queue(redis)
            for index in range(19):
                queue.enqueue_call(time.sleep,args=(0,),job_id=f'other-owner-{index}')
            self.assertEqual(queue.count,20)
            second=submit_analysis(self.ledger,document,self.owner,snapshots[1],'original')
            self.assertEqual(queue.count,20)
            self.assertNotIn(first['analysis_id'],queue.job_ids)
            self.assertIn(second['analysis_id'],queue.job_ids)

    def test_enqueue_failure_after_mode_replacement_reclaims_new_lease(self):
        document=self.create()
        stale=self.ledger.load(document,self.owner)
        environment,redis=self.queue_environment()
        with environment,self.app.app_context():
            first=submit_analysis(self.ledger,document,self.owner,dict(stale),'current')
            old_lease=self.metadata(document)['analysis']['lease']
            tile=self.ledger.reserve(self.owner,'tile',document,0)
            with patch('app.projection_layout.Queue.enqueue_call',side_effect=RedisError('controlled enqueue failure')):
                with self.assertRaises(ProjectionRejected) as rejected:
                    submit_analysis(self.ledger,document,self.owner,stale,'original')
                self.assertEqual(rejected.exception.status,503)
            latest=self.metadata(document)['analysis']
            self.assertEqual(latest['status'],'failed')
            self.assertIsNone(self.ledger.lease(latest['lease']))
            self.assertIsNone(self.ledger.lease(old_lease))
            self.assertNotIn(first['analysis_id'],projection_queue(redis).job_ids)
            self.assertTrue(self.ledger.valid_lease(tile))
            self.ledger.release(tile)

    def analyzing(self):
        document=self.create()
        meta=self.ledger.load(document,self.owner)
        meta['analysis']={'analysis_id':'controlled-analysis','status':'analyzing','phase':'queued','mode':'current'}
        self.assertTrue(self.ledger.update(document,self.owner,meta,0))
        return document,self.ledger.load(document,self.owner)

    def test_delayed_get_started_phase_cannot_overwrite_ready_worker_result(self):
        document,stale=self.analyzing()
        suggestion={'rows':1,'columns':1,'order':'row','confidence':'high'}
        def late_status(**kwargs):
            self.assertTrue(publish_analysis(self.ledger,document,self.owner,'controlled-analysis',{'status':'ready','suggestion':suggestion},0))
            self.assertEqual(self.renew(document).status_code,200)
            return 'started'
        job=SimpleNamespace(get_status=late_status,started_at=datetime.now(timezone.utc))
        with patch('app.projection_layout.projection_connection',return_value=object()),patch('app.projection_layout.Job.fetch',return_value=job):
            response=refresh_analysis_failure(self.ledger,document,self.owner,stale)
        self.assertEqual(response['status'],'ready')
        self.assertEqual(self.metadata(document)['analysis']['suggestion'],suggestion)
        # The next finished observation must retain success, not turn it failed.
        latest=self.ledger.load(document,self.owner)
        with patch('app.projection_layout.Job.fetch') as fetch:
            self.assertEqual(refresh_analysis_failure(self.ledger,document,self.owner,latest)['status'],'ready')
            fetch.assert_not_called()

    def test_delayed_get_finished_cannot_turn_published_ready_into_failed(self):
        document,stale=self.analyzing()
        def late_status(**kwargs):
            self.assertTrue(publish_analysis(self.ledger,document,self.owner,'controlled-analysis',{'status':'ready','suggestion':{'rows':1}},0))
            return 'finished'
        job=SimpleNamespace(get_status=late_status,started_at=None)
        with patch('app.projection_layout.projection_connection',return_value=object()),patch('app.projection_layout.Job.fetch',return_value=job):
            response=refresh_analysis_failure(self.ledger,document,self.owner,stale)
        self.assertEqual(response['status'],'ready')
        self.assertEqual(self.ledger.load(document,self.owner)['_generation'],0)

    def test_worker_publication_survives_phase_update_and_preserves_latest_renewal(self):
        document,stale=self.analyzing()
        deadline=stale['expires_at']
        with patch('app.projection.time.time',return_value=deadline-1):self.assertEqual(self.renew(document).status_code,200)
        with patch('app.projection_layout.projection_connection',return_value=object()),patch('app.projection_layout.Job.fetch',return_value=SimpleNamespace(get_status=lambda **kw:'started',started_at=datetime.now(timezone.utc))):
            self.assertEqual(refresh_analysis_failure(self.ledger,document,self.owner,stale)['phase'],'analyzing')
        self.assertTrue(publish_analysis(self.ledger,document,self.owner,'controlled-analysis',{'status':'ready','suggestion':{'rows':1}},0))
        self.assertEqual(self.metadata(document)['expires_at'],deadline-1+3600)
        self.assertEqual(self.metadata(document)['analysis']['status'],'ready')

    def test_late_worker_does_not_replace_manual_generation_or_new_analysis(self):
        for action in ('manual','new_analysis','delete'):
            document,stale=self.analyzing()
            if action=='delete':self.assertTrue(self.ledger.delete(document,self.owner))
            else:
                current=self.ledger.load(document,self.owner)
                if action=='manual':current.pop('analysis');current['layout']={'rows':1}
                else:current['analysis']={'analysis_id':'new-id','status':'analyzing'}
                self.assertTrue(self.ledger.update(document,self.owner,current,0,invalidate=action=='manual'))
            self.assertFalse(publish_analysis(self.ledger,document,self.owner,'controlled-analysis',{'status':'ready'},0))
            if action!='delete':self.assertTrue(self.ledger.delete(document,self.owner))

    def test_real_parallel_poll_resumes_after_worker_ready_publication(self):
        document,stale=self.analyzing()
        reading=threading.Event();resume=threading.Event();results=[];errors=[]
        def status(**kwargs):
            reading.set()
            if not resume.wait(2):raise AssertionError('controlled poll timed out')
            return 'started'
        job=SimpleNamespace(get_status=status,started_at=datetime.now(timezone.utc))
        def poll():
            try:results.append(refresh_analysis_failure(self.ledger,document,self.owner,stale))
            except Exception as error:errors.append(error)
        with patch('app.projection_layout.projection_connection',return_value=object()),patch('app.projection_layout.Job.fetch',return_value=job):
            thread=threading.Thread(target=poll);thread.start()
            try:
                self.assertTrue(reading.wait(1))
                self.assertTrue(publish_analysis(self.ledger,document,self.owner,'controlled-analysis',{'status':'ready','suggestion':{'rows':1}},0))
                self.assertEqual(self.renew(document).status_code,200)
            finally:resume.set();thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[]);self.assertEqual(results[0]['status'],'ready')
        self.assertEqual(self.metadata(document)['analysis']['status'],'ready')

    def test_phase_is_monotonic_and_terminal_cannot_be_changed_by_stale_guard(self):
        document,stale=self.analyzing()
        self.assertTrue(self.ledger.update_analysis(document,self.owner,'controlled-analysis',0,{'phase':'analyzing'}))
        self.assertFalse(self.ledger.update_analysis(document,self.owner,'controlled-analysis',0,{'phase':'queued'}))
        self.assertEqual(self.metadata(document)['analysis']['phase'],'analyzing')
        self.assertTrue(publish_analysis(self.ledger,document,self.owner,'controlled-analysis',{'status':'ready'},0))
        self.assertFalse(self.ledger.update_analysis(document,self.owner,'controlled-analysis',0,{'status':'failed'},invalidate=True))
        self.assertFalse(self.ledger.update_analysis(document,self.owner,'controlled-analysis',0,{'phase':'analyzing'}))
        self.assertEqual(self.metadata(document)['analysis']['status'],'ready')
        self.assertEqual(self.ledger.load(document,self.owner)['_generation'],0)
