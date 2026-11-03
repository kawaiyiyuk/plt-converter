"""Private local prototype: disk-backed FIFO, one CPU worker, explicit policy.

This temporary queue is not an original-source library. Operators must configure
all capacity/retention limits; this module defines no implicit user count cap.
"""
import concurrent.futures
import fcntl
import hmac
import hashlib
import json
import multiprocessing
import os
import queue as queue_module
import signal
import sys
import math
from pathlib import Path
import shutil
import threading
import time
import uuid
from flask import Flask, Response, jsonify, request
from .pattern_layout_errors import MESSAGES, public_task_error


def _worker_init():
    for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[name]='1'
    if hasattr(os,'sched_getaffinity'):
        available=os.sched_getaffinity(0)
        os.sched_setaffinity(0,{min(available)})


def _task_process(folder_string,memory_bytes,wall_seconds,supervisor_pid):
    os.setsid()
    if sys.platform.startswith('linux'):
        import ctypes,resource
        # The signal is installed before work starts and checked against the
        # expected supervisor, so a dead parent never leaves native GEOS running.
        if ctypes.CDLL(None).prctl(1,signal.SIGKILL)!=0:raise RuntimeError('cannot supervise child lifetime')
        if os.getppid()!=supervisor_pid:os._exit(125)
        resource.setrlimit(resource.RLIMIT_AS,(memory_bytes,memory_bytes))
        cpu=max(1,math.ceil(wall_seconds))
        resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu))
    elif os.getppid()!=supervisor_pid:os._exit(125)
    os.environ.pop('PATTERN_LAYOUT_SERVICE_KEY',None)
    _worker_init()
    from .projection_store import process_identity
    folder=Path(folder_string);meta=json.loads((folder/'job.json').read_text())
    meta.update(worker_pid=os.getpid(),worker_identity=process_identity(os.getpid()),memory_limit_enforced=sys.platform.startswith('linux'))
    _atomic_json(folder/'job.json',meta)
    _execute(folder_string)


def _terminate_child(process):
    if process.pid is None:return
    if process.is_alive():
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:process.terminate()
        process.join(.25)
    if process.is_alive():
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:process.kill()
    process.join()  # The queue CPU slot remains owned until confirmed reap.


def _atomic_json(path,data):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('w') as stream:
        json.dump(data,stream,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def retained_bytes(folder):
    # Pending work reserves the configured worst case. Once the writer is reaped
    # or has atomically finalized, only actual retained bytes plus metadata slack
    # stay charged. Reading/downloading never consumes the recoverable artifact.
    return 4096+sum(path.stat().st_size for path in Path(folder).rglob('*') if path.is_file())


def _execute(folder_string):
    # A restarted parent may briefly overlap an orphan worker. This disk lock
    # preserves one active computation across that transition as well.
    folder=Path(folder_string)
    with (folder.parent/'worker-cpu.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        meta=json.loads((folder/'job.json').read_text())
        if meta['status'] in ('done','failed','cancelled'):return meta['id']
        return _execute_locked(folder_string)


def _execute_locked(folder_string):
    from .services.pattern_layout_api import export_project
    from .services.pattern_layout_variants import recognize_with_variants as recognize_source
    folder=Path(folder_string);meta=json.loads((folder/'job.json').read_text())
    if (folder/'cancel').exists():
        meta.update(status='cancelled',finished_at=time.time());_atomic_json(folder/'job.json',meta);return meta['id']
    meta.update(status='processing',started_at=time.time());_atomic_json(folder/'job.json',meta)
    try:
        raw=(folder/'source.plt').read_bytes()
        if meta['kind']=='recognize':
            result=recognize_source(raw,meta['filename'])
            from .services.pattern_layout_resources import write_resources
            output_bytes=write_resources(folder,result,meta['result_budget_bytes'])
            data=None
            archive_digest=hashlib.sha256()
            with (folder/'archive.zip').open('rb') as stream:
                while chunk:=stream.read(1024*1024):archive_digest.update(chunk)
            meta.update(archive_sha256=archive_digest.hexdigest(),archive_acknowledged=False)
        else:
            project=json.loads((folder/'project.json').read_text())
            data=export_project(raw,project)
        if (folder/'cancel').exists():
            meta.update(status='cancelled',finished_at=time.time());_atomic_json(folder/'job.json',meta);return meta['id']
        if (output_bytes if data is None else len(data))>meta['result_budget_bytes']:
            raise ValueError('operator-configured result storage budget exceeded')
        if data is not None:
            result_path=folder/'result';temporary=folder/'result.tmp'
            with temporary.open('wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            temporary.replace(result_path)
        meta.update(status='done',finished_at=time.time(),result_bytes=output_bytes if data is None else len(data))
    except Exception as error:
        shutil.rmtree(folder/'resources',ignore_errors=True)
        (folder/'index.json').unlink(missing_ok=True)
        (folder/'archive.zip').unlink(missing_ok=True)
        if (folder/'cancel').exists():meta.update(status='cancelled',finished_at=time.time())
        else:
            code,message=public_task_error(error,meta['kind'])
            meta.update(status='failed',finished_at=time.time(),error_code=code,error=message)
    meta['reserved_bytes']=retained_bytes(folder)
    _atomic_json(folder/'job.json',meta)
    return meta['id']


class DiskQueue:
    def __init__(self):
        names=('ROOT','DISK_BUDGET_BYTES','MAX_RESULT_BYTES','JOB_RETENTION_SECONDS','MAX_REQUEST_BYTES','TASK_WALL_SECONDS','TASK_MEMORY_BYTES')
        missing=[name for name in names if not os.getenv('PATTERN_LAYOUT_'+name)]
        if missing: raise ValueError('explicit pattern layout queue policy required: '+','.join(missing))
        self.root=Path(os.environ['PATTERN_LAYOUT_ROOT']);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.budget=int(os.environ['PATTERN_LAYOUT_DISK_BUDGET_BYTES']);self.result_budget=int(os.environ['PATTERN_LAYOUT_MAX_RESULT_BYTES'])
        self.retention=int(os.environ['PATTERN_LAYOUT_JOB_RETENTION_SECONDS']);self.max_request=int(os.environ['PATTERN_LAYOUT_MAX_REQUEST_BYTES'])
        self.wall_seconds=float(os.environ['PATTERN_LAYOUT_TASK_WALL_SECONDS']);self.memory_bytes=int(os.environ['PATTERN_LAYOUT_TASK_MEMORY_BYTES'])
        if not math.isfinite(self.wall_seconds) or self.retention>86400:raise ValueError('task wall budget must be finite; completed-job retention cannot exceed confirmed 24 hours')
        if min(self.budget,self.result_budget,self.retention,self.max_request,self.wall_seconds,self.memory_bytes)<=0: raise ValueError('queue policy values must be positive')
        self.process_lock=(self.root/'queue.lock').open('a+')
        try: fcntl.flock(self.process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: self.process_lock.close();raise ValueError('queue root already has a worker owner')
        self.lock=threading.RLock();self.futures={};self.pending=queue_module.Queue();self.context=multiprocessing.get_context('spawn');self.active_process=None
        self.closed=threading.Event()
        self.reap()
        # A previous process cannot be trusted as still running. Its task is
        # restarted from the byte-identical source, with the retry visible.
        pending=[]
        for folder in self.root.iterdir():
            if not folder.is_dir(): continue
            meta=self.read(folder.name)
            if meta and meta['status'] in ('done','failed','cancelled'):
                meta['reserved_bytes']=retained_bytes(folder)
                _atomic_json(folder/'job.json',meta)
            if meta and meta['status']=='cancelling':
                meta.update(status='cancelled',finished_at=time.time());_atomic_json(folder/'job.json',meta)
            if meta and meta['status']=='processing':
                from .projection_store import process_identity
                pid=meta.get('worker_pid');identity=meta.get('worker_identity')
                if pid and identity and process_identity(pid)==identity:
                    try:os.killpg(pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                meta.update(status='failed',finished_at=time.time(),error=MESSAGES['worker_restart_interrupted'],error_code='worker_restart_interrupted',reserved_bytes=retained_bytes(folder))
                _atomic_json(folder/'job.json',meta)
            if meta and meta['status']=='queued':
                meta.update(status='queued',restart_count=meta.get('restart_count',0)+1)
                _atomic_json(folder/'job.json',meta);pending.append(meta)
        for meta in sorted(pending,key=lambda item:item['queued_at']): self._dispatch(meta['id'])
        self.supervisor=threading.Thread(target=self._work_loop,daemon=True);self.supervisor.start()
        self.reaper=threading.Thread(target=self._reap_loop,daemon=True);self.reaper.start()

    def read(self,identifier):
        if len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier): return None
        try:return json.loads((self.root/identifier/'job.json').read_text())
        except (FileNotFoundError,ValueError):return None

    def reap(self):
        with self.lock:
            now=time.time()
            for folder in self.root.iterdir():
                if not folder.is_dir():continue
                meta=self.read(folder.name)
                orphan = meta is None and len(folder.name)==32 and all(c in '0123456789abcdef' for c in folder.name) and now-folder.stat().st_mtime>self.retention
                if orphan or (meta and meta['status'] in ('done','failed','cancelled') and now-meta.get('finished_at',now)>self.retention):
                    shutil.rmtree(folder);self.futures.pop(folder.name,None)

    def _reap_loop(self):
        while not self.closed.wait(min(30,self.retention)):
            self.reap()

    def _dispatch(self,identifier):
        future=concurrent.futures.Future();self.futures[identifier]=future
        self.pending.put(identifier)
        return future

    def _work_loop(self):
        while True:
            identifier=self.pending.get()
            if identifier is None:return
            future=self.futures.get(identifier)
            if future is None or not future.set_running_or_notify_cancel():continue
            folder=self.root/identifier;process=None;failure=None
            try:
                if (folder/'cancel').exists():failure='cancelled'
                else:
                    process=self.context.Process(target=_task_process,args=(str(folder),self.memory_bytes,self.wall_seconds,os.getpid()))
                    self.active_process=process;process.start();deadline=time.monotonic()+self.wall_seconds
                    while process.is_alive():
                        if (folder/'cancel').exists():failure='cancelled';break
                        if time.monotonic()>=deadline:failure='wall_time_exceeded';break
                        process.join(.025)
                    _terminate_child(process)
                    if process.exitcode and failure is None:failure='worker_crash_or_memory_budget'
                with self.lock:
                    meta=self.read(identifier)
                    if meta and (failure or meta['status'] not in ('done','failed','cancelled')):
                        code=failure or 'worker_did_not_finalize'
                        public_code='task_cancelled' if code=='cancelled' else code
                        meta.update(status='cancelled' if code=='cancelled' else 'failed',finished_at=time.time(),error_code=public_code,error=MESSAGES.get(public_code,MESSAGES['supervisor_error']),reserved_bytes=retained_bytes(folder))
                        _atomic_json(folder/'job.json',meta)
                    if meta and meta['status'] in ('done','failed','cancelled'):
                        meta['reserved_bytes']=retained_bytes(folder)
                        _atomic_json(folder/'job.json',meta)
                future.set_result(identifier)
            except BaseException as error:
                if process is not None:_terminate_child(process)
                meta=self.read(identifier)
                if meta:
                    meta.update(status='failed',finished_at=time.time(),error_code='supervisor_error',error=MESSAGES['supervisor_error'],reserved_bytes=retained_bytes(folder))
                    _atomic_json(folder/'job.json',meta)
                future.set_result(identifier)
            finally:
                self.active_process=None
                if (folder/'discard').exists():
                    with self.lock:
                        shutil.rmtree(folder);self.futures.pop(identifier,None)

    def submit(self,upload,kind,owner,project=None,idempotency_key=None):
        # Werkzeug has already spooled large uploads to disk. Determine file
        # length without reading each queued source into Python/RQ memory.
        stream=upload.stream;stream.seek(0,2);size=stream.tell();stream.seek(0)
        project_data=json.dumps(project,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode() if project is not None else b''
        digest=hashlib.sha256()
        while chunk:=stream.read(1024*1024):digest.update(chunk)
        stream.seek(0)
        content_key=hashlib.sha256((kind+':'+digest.hexdigest()).encode()+project_data).hexdigest()
        request_key=hashlib.sha256((idempotency_key or content_key).encode()).hexdigest()
        reserved=size+len(project_data)+self.result_budget+4096
        with self.lock:
            self.reap();used=0
            for folder in self.root.iterdir():
                if folder.is_dir():
                    meta=self.read(folder.name)
                    if meta:
                        used+=meta['reserved_bytes']
                        if meta.get('owner_sha256')==owner and meta.get('request_key')==request_key and (idempotency_key or meta['status'] in ('queued','processing','done')):
                            if meta['content_key']!=content_key:raise ValueError('idempotency key reused with different payload')
                            # An ACKed recognition keeps a light operation tombstone,
                            # not reusable geometry. A new attachment with identical
                            # bytes must receive a new archive; explicit same-operation
                            # idempotency still preserves its original job identity.
                            unavailable=meta['kind']=='recognize' and meta['status']=='done' and (meta.get('archive_acknowledged') or not (folder/'archive.zip').is_file())
                            if unavailable and not idempotency_key:continue
                            return meta,self.futures.get(meta['id'])
                    else: used+=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
            if used+reserved>self.budget or shutil.disk_usage(self.root).free<reserved:
                raise OSError('operator-configured temporary queue storage unavailable')
            identifier=uuid.uuid4().hex;folder=self.root/identifier;folder.mkdir(mode=0o700)
            meta={'id':identifier,'status':'queued','kind':kind,'queued_at':time.time(),'filename':Path(upload.filename or 'source.plt').name,
                  'owner_sha256':owner,'request_key':request_key,'content_key':content_key,
                  'reserved_bytes':reserved,'result_budget_bytes':self.result_budget,'retention_seconds':self.retention,'restart_count':0}
            try:
                with (folder/'source.plt').open('xb') as target:shutil.copyfileobj(stream,target,1024*1024)
                if project is not None:(folder/'project.json').write_bytes(project_data)
                _atomic_json(folder/'job.json',meta);future=self._dispatch(identifier)
            except Exception: shutil.rmtree(folder);raise
            return meta,future

    def cancel(self,identifier):
        with self.lock:
            meta=self.read(identifier)
            if meta is None:return None
            future=self.futures.get(identifier)
            if meta['status'] in ('queued','processing','cancelling'):
                (self.root/identifier/'cancel').touch()
                if future is not None and future.cancel():
                    meta.update(status='cancelled',finished_at=time.time(),reserved_bytes=retained_bytes(self.root/identifier))
                    _atomic_json(self.root/identifier/'job.json',meta)
                    return 'cancelled'
                # The marker is the only cross-process cancellation write.
                # Worker owns job.json while running, avoiding lost-state races.
                return 'cancelling'
            shutil.rmtree(self.root/identifier);self.futures.pop(identifier,None);return True

    def shutdown(self,wait=True,cancel_futures=False):
        if cancel_futures:
            for identifier in list(self.futures):
                meta=self.read(identifier)
                if meta and meta['status'] in ('queued','processing'):(self.root/identifier/'cancel').touch()
        self.closed.set();self.reaper.join(timeout=1)
        self.pending.put(None)
        self.supervisor.join()  # Never release root ownership before child reap.
        fcntl.flock(self.process_lock,fcntl.LOCK_UN);self.process_lock.close()


def create_app():
    app=Flask(__name__);queue=DiskQueue();app.config['MAX_CONTENT_LENGTH']=queue.max_request
    app.extensions['pattern_layout_queue']=queue
    # Compatibility for the local test/integration harness cleanup.
    app.extensions['pattern_layout_executor']=queue
    app.extensions['pattern_layout_jobs']=queue.futures

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify(error='排版请求超过服务接收预算，请保留草稿并反馈该文件。',error_code='request_too_large',max_request_bytes=queue.max_request),413

    @app.before_request
    def authenticate():
        key=os.getenv('PATTERN_LAYOUT_SERVICE_KEY','')
        if not key:return jsonify(error=MESSAGES['configuration_required'],error_code='configuration_required'),503
        if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+key):return jsonify(error='private service authentication required'),401
        if request.path.startswith('/internal/pattern-layout/jobs') or request.path=='/internal/pattern-layout/export':
            if not request.headers.get('X-Pattern-Layout-Owner'):return jsonify(error='internal task owner required'),400

    def owner_digest():return hashlib.sha256(request.headers['X-Pattern-Layout-Owner'].encode()).hexdigest()

    def owned_meta(identifier):
        meta=queue.read(identifier)
        return meta if meta and hmac.compare_digest(meta.get('owner_sha256',''),owner_digest()) else None

    @app.get('/internal/pattern-layout/capabilities')
    def capabilities():
        from .services.pattern_layout_api import runtime_version
        from .services.pattern_layout_fingerprint import export_runtime_version
        return jsonify(export_runtime_version=export_runtime_version(),fingerprint_schema=2,schema_version=1,runtime_version=runtime_version(),engine_version='v6b-program-only-experimental-with-source-contour-alternatives',semantic_verified=False,memory_limit_enforcement='linux_rlimit_as' if sys.platform.startswith('linux') else 'unverified_non_linux',hard_cancel=True)

    @app.post('/internal/pattern-layout/jobs')
    def recognize():
        upload=request.files.get('file')
        if upload is None:return jsonify(error='file required'),400
        try:meta,_future=queue.submit(upload,'recognize',owner_digest(),idempotency_key=request.headers.get('X-Idempotency-Key'))
        except OSError as error:return jsonify(error=MESSAGES['storage_unavailable'],error_code='storage_unavailable',status='storage_unavailable'),503
        except ValueError as error:return jsonify(error=MESSAGES['idempotency_conflict'],error_code='idempotency_conflict',status='idempotency_conflict'),409
        return jsonify(id=meta['id'],status=meta['status'],retention_seconds=queue.retention),202

    @app.get('/internal/pattern-layout/jobs/<identifier>')
    def status(identifier):
        with queue.lock:
            queue.reap();meta=owned_meta(identifier)
            if meta is None:return jsonify(error='unknown or expired job'),404
            if (queue.root/identifier/'cancel').exists() and meta['status'] in ('queued','processing'):meta['status']='cancelling'
            public={key:meta[key] for key in ('id','status','queued_at','started_at','finished_at','restart_count','error','error_code','archive_sha256','archive_acknowledged','retention_seconds') if key in meta}
            if meta['status']=='done':
                if meta['kind']=='recognize':public['result']=json.loads((queue.root/identifier/'index.json').read_bytes())
                else:public['result_ready']=True
            return jsonify(public),202 if meta['status'] in ('queued','processing','cancelling') else 200

    @app.get('/internal/pattern-layout/jobs/<identifier>/source-geometry')
    @app.get('/internal/pattern-layout/jobs/<identifier>/audit')
    @app.get('/internal/pattern-layout/jobs/<identifier>/candidates/<candidate_id>')
    def resource(identifier,candidate_id=None):
        with queue.lock:
            meta=owned_meta(identifier)
            if meta is None:return jsonify(error='unknown or expired job'),404
            if meta['status']!='done' or meta['kind']!='recognize':return jsonify(error='recognition resource unavailable'),409
            suffix=request.path.rsplit('/',1)[-1]
            if candidate_id is not None:
                if not candidate_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in candidate_id):return jsonify(error='unknown candidate'),404
                path=queue.root/identifier/'resources'/'candidates'/(candidate_id+'.json.gz')
            else:path=queue.root/identifier/'resources'/('audit.json.gz' if suffix=='audit' else 'source-geometry.json.gz')
            if not path.is_file():return jsonify(error='unknown resource'),404
            return Response(path.read_bytes(),mimetype='application/json',headers={'Content-Encoding':'gzip','Cache-Control':'private, no-store'})

    @app.get('/internal/pattern-layout/jobs/<identifier>/archive')
    def archive(identifier):
        with queue.lock:
            meta=owned_meta(identifier)
            if meta is None:return jsonify(error='unknown or expired job'),404
            if meta['status']!='done' or meta['kind']!='recognize':return jsonify(error='recognition archive unavailable'),409
            path=queue.root/identifier/'archive.zip'
            if not path.is_file():return jsonify(error='archive already durably acknowledged or expired'),404
            stream=path.open('rb')
        def chunks():
            try:
                while chunk:=stream.read(64*1024):yield chunk
            finally:stream.close()
        response=Response(chunks(),mimetype='application/zip',headers={'Cache-Control':'private, no-store','X-Archive-Sha256':meta['archive_sha256']})
        response.call_on_close(stream.close)
        return response

    @app.post('/internal/pattern-layout/jobs/<identifier>/ack')
    def acknowledge_archive(identifier):
        with queue.lock:
            meta=owned_meta(identifier)
            if meta is None:return jsonify(error='unknown job'),404
            payload=request.get_json(silent=True) or {}
            if meta['kind']!='recognize' or meta['status']!='done':return jsonify(error='archive not ready'),409
            if payload.get('durable_complete') is not True or not hmac.compare_digest(str(payload.get('archive_sha256','')),meta.get('archive_sha256','')):return jsonify(error='durable archive acknowledgement mismatch'),409
            if not meta.get('archive_acknowledged'):
                meta.update(archive_acknowledged=True,archive_acknowledged_at=time.time())
                # The caller already proved the complete archive is durable.
                # Retain only light index/metadata for retry/idempotency.
                folder=queue.root/identifier
                shutil.rmtree(folder/'resources',ignore_errors=True)
                for name in ('source.plt','archive.zip'):(folder/name).unlink(missing_ok=True)
                meta['reserved_bytes']=retained_bytes(folder)
                _atomic_json(folder/'job.json',meta)
            return jsonify(id=identifier,archive_acknowledged=True)

    @app.delete('/internal/pattern-layout/jobs/<identifier>')
    def release(identifier):
        meta=owned_meta(identifier)
        if meta is None:return jsonify(error='unknown job'),404
        discard=request.args.get('discard')=='true'
        if meta['kind']=='recognize' and meta['status']=='done' and not meta.get('archive_acknowledged') and not discard:return jsonify(error='complete archive must be durably acknowledged or explicitly discarded before release'),409
        if discard:(queue.root/identifier/'discard').touch()
        cancelled=queue.cancel(identifier)
        if discard and cancelled=='cancelled':
            with queue.lock:
                shutil.rmtree(queue.root/identifier);queue.futures.pop(identifier,None)
            return '',204
        if cancelled is None:return jsonify(error='unknown job'),404
        if cancelled in ('cancelling','cancelled'):return jsonify(id=identifier,status=cancelled),202 if cancelled=='cancelling' else 200
        return '',204

    @app.post('/internal/pattern-layout/export')
    def export():
        upload=request.files.get('file')
        if upload is None:return jsonify(error='file required'),400
        try:
            project_part=request.files.get('project')
            project=json.load(project_part.stream) if project_part is not None else json.loads(request.form['project'])
        except (KeyError,ValueError,UnicodeError):return jsonify(error='请保留草稿并重新提交有效的排版文件。',error_code='invalid_project_document'),400
        try:meta,_future=queue.submit(upload,'export',owner_digest(),project,idempotency_key=request.headers.get('X-Idempotency-Key'))
        except OSError as error:return jsonify(error=MESSAGES['storage_unavailable'],error_code='storage_unavailable',status='storage_unavailable'),503
        except ValueError as error:return jsonify(error=MESSAGES['idempotency_conflict'],error_code='idempotency_conflict',status='idempotency_conflict'),409
        return jsonify(id=meta['id'],status=meta['status'],retention_seconds=queue.retention),202

    @app.get('/internal/pattern-layout/jobs/<identifier>/result')
    def download_result(identifier):
        with queue.lock:
            meta=owned_meta(identifier)
            if meta is None:return jsonify(error='unknown or expired job'),404
            if meta['kind']!='export' or meta['status']!='done':return jsonify(error='export result not ready',status=meta['status']),409
            return Response((queue.root/identifier/'result').read_bytes(),mimetype='application/vnd.hp-hpgl',headers={'Content-Disposition':'attachment; filename="layout.plt"','Cache-Control':'private, no-store'})

    return app
