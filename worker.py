import os
import threading
import time
import logging

from rq import Worker

from app.job_queue import (
    cleanup_expired_job_files,
    conversion_queue,
    pdf_layout_queue,
    redis_connection,
)
from app.tasks import reconcile_finalizing_jobs


LOGGER = logging.getLogger(__name__)


def worker_queues(connection, role=None):
    """Return the single queue assigned to this worker process."""
    selected_role = (role or os.getenv('PLT_WORKER_ROLE', 'conversion')).strip().lower()
    if selected_role == 'conversion':
        return [conversion_queue(connection)]
    if selected_role == 'layout':
        return [pdf_layout_queue(connection)]
    raise ValueError(f'不支持的 Worker 角色：{selected_role}')


def cleanup_loop():
    while True:
        try:
            cleanup_expired_job_files()
            reconcile_finalizing_jobs()
        except Exception as error:
            LOGGER.warning('Temporary job cleanup failed: %s', error)
        time.sleep(300)


if __name__ == '__main__':
    connection = redis_connection(blocking=True)
    role = os.getenv('PLT_WORKER_ROLE', 'conversion').strip().lower()
    if role == 'conversion':
        threading.Thread(target=cleanup_loop, daemon=True).start()
    Worker(worker_queues(connection, role), connection=connection).work(with_scheduler=False)
