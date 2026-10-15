from types import SimpleNamespace
import unittest
from unittest.mock import patch
from rq import Worker as ActualRQWorker
from app.projection_layout import projection_connection,worker_main,QUEUE_NAME
import test_projection as fixtures

class ProjectionWorkerConnectionTests(unittest.TestCase):
    setUp=fixtures.ProjectionTests.setUp
    identity=fixtures.ProjectionTests.identity

    def test_worker_main_uses_separate_rq_blocking_pool_without_changing_api_pool(self):
        workers=[]
        def build_worker(queues,connection):
            # Real RQ init/configuration, without network work/client registration.
            worker=ActualRQWorker(queues,connection=connection,prepare_for_work=False)
            workers.append(worker)
            return SimpleNamespace(work=lambda:None)
        with self.app.app_context():api=projection_connection()
        with patch('app.projection_app.create_projection_app',return_value=self.app),patch('app.projection_layout.Worker',side_effect=build_worker):
            worker_main()
        worker=workers[0]
        self.assertEqual([queue.name for queue in worker.queues],[QUEUE_NAME])
        self.assertGreater(worker.connection.connection_pool.connection_kwargs['socket_timeout'],worker.dequeue_timeout)
        self.assertEqual(worker.connection.connection_pool.connection_kwargs['socket_timeout'],worker.connection_timeout)
        self.assertEqual(worker.connection.connection_pool.connection_kwargs['socket_connect_timeout'],2)
        self.assertIsNot(worker.connection.connection_pool,api.connection_pool)
        self.assertEqual(api.connection_pool.connection_kwargs['socket_timeout'],2)

    def test_url_query_cannot_override_short_api_or_worker_blocking_contract(self):
        self.app.config['PROJECTOR_REDIS_URL']='redis://localhost:6399/0?socket_timeout=99&socket_connect_timeout=99'
        with self.app.app_context():
            api=projection_connection()
            blocking=projection_connection(blocking=True)
        self.assertIsNone(blocking.connection_pool.connection_kwargs['socket_timeout'])
        worker=ActualRQWorker([QUEUE_NAME],connection=blocking,prepare_for_work=False)
        self.assertEqual(worker.connection.connection_pool.connection_kwargs['socket_timeout'],worker.connection_timeout)
        self.assertEqual(api.connection_pool.connection_kwargs['socket_timeout'],2)
        self.assertEqual(api.connection_pool.connection_kwargs['socket_connect_timeout'],2)
