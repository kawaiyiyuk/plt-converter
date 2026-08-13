import threading
import time
import logging

from rq import Worker

from app.job_queue import cleanup_expired_job_files, conversion_queue, redis_connection


LOGGER = logging.getLogger(__name__)


def cleanup_loop():
    while True:
        try:
            cleanup_expired_job_files()
        except Exception as error:
            LOGGER.warning('Temporary job cleanup failed: %s', error)
        time.sleep(300)


if __name__ == '__main__':
    threading.Thread(target=cleanup_loop, daemon=True).start()
    connection = redis_connection(blocking=True)
    queue = conversion_queue(connection)
    Worker([queue], connection=connection).work(with_scheduler=False)
