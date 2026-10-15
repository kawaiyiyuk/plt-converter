from datetime import datetime,timezone
from types import SimpleNamespace
import time
import threading
import unittest
from unittest.mock import patch
import test_projection as fixtures
import test_projection_resources as resources
from app.projection_layout import publish_analysis,refresh_analysis_failure

class AnalysisCASTests(unittest.TestCase):
    setUp=fixtures.ProjectionTests.setUp
    identity=fixtures.ProjectionTests.identity
    upload=fixtures.ProjectionTests.upload
    tile=fixtures.ProjectionTests.tile
    metadata=fixtures.ProjectionTests.metadata
    renew=fixtures.ProjectionTests.renew
    create=resources.ResourceTests.create
    policy_restore=resources.ResourceTests.policy_restore

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
