"""Projection-only SQLite admission and session ledger (single local disk volume).

Transactions never span parsing, rendering, Redis, authentication or body reads.
Bytes reserved are block-rounded worst-case allocations, including SQLite and its
rollback journal. Rows/files with unfinished leases are retained after deletion.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time

MAX_SOURCE = 20 * 1024 * 1024
MAX_BODY = MAX_SOURCE + 1024 * 1024
# Metadata derives from the complete accepted source size, not a smaller page
# count. 200-page grids and embedded metadata fit without another page limit.
META_BOUND = 200 * 1024 + (24 * 24) * 128 + 65536
PNG_BOUND = 4_000_000 * 3 + 65536
FIELDS = ('OWNER_MAX_DOCUMENTS OWNER_SOURCE_BYTES GLOBAL_DISK_BYTES FREE_FLOOR_BYTES '
          'UPLOAD_OWNER_RATE UPLOAD_IP_RATE TILE_RATE TILE_BURST OWNER_RENDERERS '
          'GLOBAL_RENDERERS GLOBAL_PARSERS PARSE_WALL_SECONDS TILE_WALL_SECONDS '
          'CHILD_CPU_SECONDS CHILD_MEMORY_BYTES').split()

class ProjectionRejected(Exception):
    def __init__(self, code, message, status=429, retry_after=1):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after

class Policy:
    def __init__(self, mapping):
        for field in FIELDS:
            key = 'PROJECTOR_' + field
            value = mapping.get(key)
            if isinstance(value, bool) or value is None or str(value).strip() != str(value):
                raise ValueError(f'{key} 必须显式设置为正整数')
            try:
                number = int(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f'{key} 必须显式设置为正整数') from error
            if number < 1 or number > 2**53-1 or str(number) != str(value):
                raise ValueError(f'{key} 必须显式设置为正整数')
            setattr(self, field.lower(), number)
        # Old PDF-only configurations remain valid. PLT admission explicitly
        # requires both separate derived budgets rather than inheriting source.
        for field in ('DERIVED_MAX_BYTES', 'OWNER_DERIVED_BYTES'):
            value = mapping.get('PROJECTOR_' + field)
            if value is None:
                setattr(self, field.lower(), 0)
                continue
            if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 2**53-1:
                raise ValueError(f'PROJECTOR_{field} 必须显式设置为正整数')
            setattr(self, field.lower(), int(value))
        if self.derived_max_bytes and self.owner_derived_bytes and self.owner_derived_bytes < self.derived_max_bytes:
            raise ValueError('派生文件 owner 额度不能小于单文件上限')
        if self.owner_source_bytes < MAX_SOURCE:
            raise ValueError('源额度必须可容纳既有20MiB文件')
        if self.owner_renderers > self.global_renderers:
            raise ValueError('owner renderers cannot exceed global renderers')
        # Database and rollback journal are each hard-bounded by max_page_count.
        self.db_bytes = (self.global_disk_bytes // 16 // 4096) * 4096
        self.system_bytes = 2 * self.db_bytes + 65536
        self.upload_bytes = MAX_BODY + 2 * META_BOUND + 65536
        self.operation_bytes = 2 * META_BOUND + PNG_BOUND + 65536
        self.render_pool_bytes = self.global_renderers * self.operation_bytes
        if self.db_bytes < 1024 * 1024 or self.global_disk_bytes < self.system_bytes + self.upload_bytes + self.render_pool_bytes:
            raise ValueError('global budget cannot hold state/control + one upload + renderer pool')
        if self.child_memory_bytes < MAX_SOURCE + META_BOUND + PNG_BOUND:
            raise ValueError('child memory cannot hold accepted source and output bounds')
        self.rate_rows = self.db_bytes // 4096 // 8
        if self.db_bytes // 4096 > 4294967294:
            raise ValueError('SQLite state budget exceeds supported page limit')


def allocation(value):
    return ((int(value) + 4095) // 4096) * 4096


def process_identity(pid):
    """Linux start ticks are stable across PID reuse. Other hosts are test-only."""
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        return text[text.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        return None


def process_alive(pid, identity):
    if pid == -1:
        return True  # Claimed launch: only supervisor/reconciliation may clear.
    if not pid:
        return False
    actual = process_identity(pid)
    if actual is not None:
        return actual == identity
    if identity:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reserved_budget(db):
    return db.execute('SELECT COALESCE(SUM(budget),0) FROM leases').fetchone()[0]


class Store:
    def __init__(self, root, policy):
        self.root, self.policy = Path(root), policy
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for folder in ('staging', 'operations', 'documents', 'metadata'):
            (self.root / folder).mkdir(mode=0o700, exist_ok=True)
        self.database = self.root / 'state.sqlite3'
        with self.transaction() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL,
              generation INTEGER NOT NULL, state TEXT NOT NULL, source_bytes INTEGER NOT NULL,
              meta TEXT NOT NULL, budget INTEGER NOT NULL);
              CREATE INDEX IF NOT EXISTS session_owner ON sessions(owner,state);
              CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
              kind TEXT NOT NULL, document TEXT, generation INTEGER, budget INTEGER NOT NULL,
              source_budget INTEGER NOT NULL, pid INTEGER, identity TEXT, supervisor INTEGER,
              supervisor_identity TEXT, created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS lease_document ON leases(document);
              CREATE TABLE IF NOT EXISTS rates(key TEXT PRIMARY KEY,tokens REAL NOT NULL,
              updated REAL NOT NULL,expires REAL NOT NULL);''')
        with self.transaction() as db:
            # Additive migration keeps already-active PDF sessions usable.
            for table, column in (('sessions', 'derived_bytes'), ('leases', 'derived_budget')):
                if column not in {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}:
                    db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' INTEGER NOT NULL DEFAULT 0')
        self.reconcile()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.database, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA max_page_count=' + str(self.policy.db_bytes // 4096))
            db.execute('PRAGMA journal_size_limit=' + str(self.policy.db_bytes))
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _expire(self, db):
        db.execute("UPDATE sessions SET state='deleted',generation=generation+1 WHERE state='active' AND expires<=?", (time.time(),))
        for row in db.execute("SELECT id,meta FROM sessions WHERE state='deleted' AND NOT EXISTS(SELECT 1 FROM leases WHERE document=sessions.id)").fetchall():
            path = self.source_path(row['id'])
            try:
                path.unlink(missing_ok=True)
                for file in (self.root/'metadata').glob(row['id']+'.*.json'):
                    file.unlink(missing_ok=True)
            except OSError:
                continue  # Keep its budget until the actual deletion succeeds.
            db.execute('DELETE FROM sessions WHERE id=?', (row['id'],))

    def source_path(self, document):
        return self.root / 'documents' / (document + '.pdf')

    def _disk_accounting(self,db):
        """Charge each bucket once, including unexpected leftover allocations.

        Renderer slots consume a fixed global disk pool; uploading/analyzing
        cannot take that pool. Actual bytes above any reservation still count.
        The unwritten portion of every bucket protects the statvfs free floor.
        """
        sessions={r['id']:r['budget'] for r in db.execute('SELECT id,budget FROM sessions')}
        leases={r['id']:dict(r) for r in db.execute('SELECT id,kind,budget FROM leases')}
        budgets={'system':self.policy.system_bytes,'render_pool':self.policy.render_pool_bytes}
        budgets.update({'session:'+key:value for key,value in sessions.items()})
        budgets.update({'lease:'+key:row['budget'] for key,row in leases.items() if row['kind']!='tile'})
        actual={key:0 for key in budgets}
        for folder,dirs,files in os.walk(self.root):
            for name in [None]+files:
                path=Path(folder) if name is None else Path(folder)/name
                relative=path.relative_to(self.root).parts
                key='untracked'
                if not relative or (len(relative)==1 and relative[0] in ('staging','operations','documents','metadata')):
                    key='system'
                elif relative[0].startswith('state.sqlite3') and len(relative)==1:
                    key='system'
                elif relative[0]=='documents' and len(relative)==2 and path.stem in sessions:
                    key='session:'+path.stem
                elif relative[0]=='metadata' and len(relative)==2 and relative[1].split('.',1)[0] in sessions:
                    key='session:'+relative[1].split('.',1)[0]
                elif relative[0] in ('staging','operations') and len(relative)>=2 and relative[1] in leases:
                    row=leases[relative[1]]
                    key='render_pool' if row['kind']=='tile' else 'lease:'+relative[1]
                try:actual[key]=actual.get(key,0)+path.stat().st_blocks*512
                except FileNotFoundError:pass
        used=sum(max(budgets.get(key,0),value) for key,value in actual.items())
        unwritten=sum(max(value-actual.get(key,0),0) for key,value in budgets.items())
        return used,unwritten

    def _capacity(self, db, budget, kind):
        used,unwritten=self._disk_accounting(db)
        additional=0 if kind=='tile' else budget
        if used + additional > self.policy.global_disk_bytes:
            raise ProjectionRejected('projection_capacity', '投影存储空间已满，请稍后重试')
        disk=os.statvfs(self.root)
        free=disk.f_bavail*disk.f_frsize
        if free-unwritten-additional < self.policy.free_floor_bytes:
            raise ProjectionRejected('projection_disk_floor','投影磁盘可用空间不足',503)

    def rate(self, owner, ip, kind):
        now = time.time()
        with self.transaction() as db:
            db.execute('DELETE FROM rates WHERE expires<=?', (now,))
            specs = [(f'{kind}:owner:{owner}', self.policy.tile_rate if kind == 'tile' else self.policy.upload_owner_rate,
                      self.policy.tile_burst if kind == 'tile' else self.policy.upload_owner_rate,
                      1 if kind == 'tile' else 60)]
            if kind == 'upload':
                specs.append((f'upload:ip:{ip}', self.policy.upload_ip_rate, self.policy.upload_ip_rate, 60))
            updates = []
            for raw, rate, burst, period in specs:
                key = hashlib.sha256(raw.encode()).hexdigest()
                row = db.execute('SELECT * FROM rates WHERE key=?', (key,)).fetchone()
                if row is None and db.execute('SELECT COUNT(*) FROM rates').fetchone()[0] + len(updates) >= self.policy.rate_rows:
                    raise ProjectionRejected('projection_limiter_capacity', '投影请求记录已满，请稍后重试', 429, 60)
                if kind=='upload':
                    window=math.floor(now/60)*60
                    tokens=burst if row is None or row['updated']!=window else row['tokens']
                    retry=max(1,math.ceil(window+60-now))
                    updated,expires=window,window+120
                else:
                    tokens=burst if row is None else min(burst,row['tokens']+max(0,now-row['updated'])*rate/period)
                    retry=max(1,math.ceil((1-tokens)*period/rate))
                    updated,expires=now,now+period*2
                if tokens<1:
                    raise ProjectionRejected('projection_rate','投影请求过快，请稍后重试',429,retry)
                updates.append((key,tokens-1,updated,expires))
            db.executemany('INSERT OR REPLACE INTO rates VALUES(?,?,?,?)', updates)

    def reserve(self, owner, kind, document=None, generation=None):
        lease = secrets.token_hex(32)
        policy = self.policy
        with self.transaction() as db:
            self._expire(db)
            source_budget = MAX_SOURCE if kind == 'upload' else 0
            if kind == 'upload':
                count = db.execute("SELECT COUNT(*) FROM sessions WHERE owner=?", (owner,)).fetchone()[0]
                # An upload that is still parsing (kind='upload-parse') has no
                # session yet, but it already owns a document slot; count it so
                # a second device cannot slip past the per-owner document limit.
                # Superseded uploads (generation=-1) are unwinding and no longer
                # hold a slot, so they are excluded here.
                count += db.execute(
                    "SELECT COUNT(*) FROM leases WHERE owner=? AND kind IN ('upload','upload-parse') "
                    "AND COALESCE(generation,0)!=-1", (owner,)).fetchone()[0]
                source = db.execute('SELECT COALESCE(SUM(source_bytes),0) FROM sessions WHERE owner=?', (owner,)).fetchone()[0]
                source += db.execute(
                    "SELECT COALESCE(SUM(source_budget),0) FROM leases WHERE owner=? AND kind IN ('upload','upload-parse') "
                    "AND COALESCE(generation,0)!=-1", (owner,)).fetchone()[0]
                if count >= policy.owner_max_documents or source + source_budget > policy.owner_source_bytes:
                    raise ProjectionRejected('projection_owner_capacity', '投影文件数量或累计源文件额度已满')
                # A superseded upload still holds its staging bytes until its
                # parse child (if any) unwinds. Cap how many one account may
                # leave unwinding so back-to-back uploads cannot exhaust the
                # shared global disk.
                superseded = db.execute(
                    "SELECT COUNT(*) FROM leases WHERE owner=? AND generation=-1 "
                    "AND kind IN ('upload','upload-parse')", (owner,)).fetchone()[0]
                if superseded >= policy.owner_max_documents:
                    raise ProjectionRejected('projection_owner_capacity', '投影文件数量或累计源文件额度已满')
                # Upload reserves owner/disk budget only. The global parser slot
                # is taken by begin_parse() after the body arrives, so one slow
                # client cannot occupy a parser (see begin_parse).
                budget = policy.upload_bytes
            else:
                session = self._load(db, document, owner)
                if session is None:
                    raise ProjectionRejected('projection_not_found', '投影文件不存在或已过期', 404, 0)
                if session['generation'] != generation:
                    raise ProjectionRejected('projection_result_stale', '投影会话已变化，请重新加载最新画面', 409, 0)
                if kind == 'tile':
                    global_count = db.execute("SELECT COUNT(*) FROM leases WHERE kind='tile'").fetchone()[0]
                    owner_count = db.execute("SELECT COUNT(*) FROM leases WHERE kind='tile' AND owner=?", (owner,)).fetchone()[0]
                    if global_count >= policy.global_renderers or owner_count >= policy.owner_renderers:
                        raise ProjectionRejected('projection_renderer_busy', '投影渲染繁忙，正在等待最新画面')
                budget = policy.operation_bytes
            self._capacity(db, budget,kind)
            db.execute('INSERT INTO leases(id,owner,kind,document,generation,budget,source_budget,pid,identity,supervisor,supervisor_identity,created) VALUES(?,?,?,?,?,?,?,NULL,NULL,?,?,?)',
                       (lease, owner, kind, document, generation, budget, source_budget,
                        os.getpid(), process_identity(os.getpid()), time.time()))
            if kind == 'upload':
                # Last initiated upload wins: mark every other in-flight upload
                # of this owner superseded in the same transaction. Their parse
                # children are terminated by valid_lease within one poll and
                # their slots/staging released by the route's finally block.
                db.execute("UPDATE leases SET generation=-1 WHERE owner=? AND id!=? "
                           "AND kind IN ('upload','upload-parse')", (owner, lease))
        return lease

    def reserve_derived(self, lease):
        """Charge PLT's extra output before a child can create that PDF."""
        maximum = self.policy.derived_max_bytes
        if not maximum or not self.policy.owner_derived_bytes:
            raise ProjectionRejected('projection_plt_unconfigured', 'PLT 投影派生文件预算尚未配置', 503)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM leases WHERE id=?', (lease,)).fetchone()
            if row is None or row['generation'] == -1:
                raise ProjectionRejected('projection_upload_superseded', '已选择更新的纸样，本次上传已取消', 409, 0)
            if row['derived_budget']:
                return
            actual = db.execute('SELECT COALESCE(SUM(derived_bytes),0) FROM sessions WHERE owner=?', (row['owner'],)).fetchone()[0]
            pending = db.execute('SELECT COALESCE(SUM(derived_budget),0) FROM leases WHERE owner=?', (row['owner'],)).fetchone()[0]
            if actual + pending + maximum > self.policy.owner_derived_bytes:
                raise ProjectionRejected('projection_owner_derived_capacity', 'PLT 投影临时派生文件额度已满，请稍后重试')
            budget = allocation(maximum)
            self._capacity(db, budget, 'upload')
            db.execute('UPDATE leases SET derived_budget=?,budget=budget+? WHERE id=?', (maximum, budget, lease))

    def begin_parse(self, lease):
        """Atomically admit a received upload to the bounded global parser pool.

        Upload leases never occupy a parser slot before their body exists, so a
        slow client cannot make every other upload see projection_parser_busy.
        The slot is taken (kind='upload-parse') only once the source is complete.
        On rejection the caller releases the upload lease and its staged file.

        A superseded upload (generation=-1) still holds its slot until its parse
        child unwinds (a ~50ms poll plus a 0.25s grace). A user who quickly
        reselects a small file would otherwise hit projection_parser_busy twice,
        so while at least one superseded upload-parse is still unwinding we wait
        briefly and retry every check, up to 1.5s.
        """
        deadline = time.monotonic() + 1.5
        while True:
            delay = False
            with self.transaction() as db:
                row = db.execute('SELECT * FROM leases WHERE id=?', (lease,)).fetchone()
                if row is None:
                    raise ProjectionRejected('projection_upload_lost', '投影上传状态已失效', 503)
                if row['generation'] == -1:
                    # A newer upload replaced this one; never take a parser slot.
                    raise ProjectionRejected('projection_upload_superseded','已选择更新的纸样，本次上传已取消',409,0)
                if row['kind'] == 'upload-parse':
                    return  # Already admitted; keep it idempotent.
                if row['kind'] != 'upload':
                    raise ProjectionRejected('projection_upload_lost', '投影上传状态已失效', 503)
                active = db.execute("SELECT COUNT(*) FROM leases WHERE kind IN ('parse','upload-parse')").fetchone()[0]
                if active >= self.policy.global_parsers:
                    unwinding = db.execute(
                        "SELECT COUNT(*) FROM leases WHERE kind='upload-parse' AND generation=-1").fetchone()[0]
                    if not unwinding or time.monotonic() >= deadline:
                        raise ProjectionRejected('projection_parser_busy', '投影解析繁忙，请稍后重试')
                    delay = True
                else:
                    db.execute("UPDATE leases SET kind='upload-parse' WHERE id=?", (lease,))
                    return
            if delay:
                time.sleep(0.05)

    def lease_path(self, lease):
        row = self.lease(lease)
        if row is None:
            # A concurrent DELETE may release an unclaimed lease between reserve
            # and rendering; report a lost lease instead of a TypeError.
            raise ProjectionRejected('projection_lease_lost', '投影任务已失效', 409, 0)
        return self.root / 'staging' / lease if row['kind'].startswith('upload') else self.root / 'operations' / lease

    def lease(self, lease):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM leases WHERE id=?', (lease,)).fetchone()
            return dict(row) if row else None

    def claim(self,lease):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM leases WHERE id=?',(lease,)).fetchone()
            if row is None or row['pid'] is not None:
                raise ProjectionRejected('projection_lease_lost','投影任务已失效',409,0)
            db.execute('UPDATE leases SET pid=-1,supervisor=?,supervisor_identity=? WHERE id=?',
                       (os.getpid(),process_identity(os.getpid()),lease))

    def attach(self, lease, pid):
        with self.transaction() as db:
            db.execute('UPDATE leases SET pid=?,identity=? WHERE id=?', (pid, process_identity(pid), lease))

    def valid_lease(self, lease):
        with self.transaction() as db:
            self._expire(db)
            row = db.execute('SELECT * FROM leases WHERE id=?', (lease,)).fetchone()
            if row is None:
                return False
            if row['kind'].startswith('upload'):
                # A superseded upload must stop polling; its child is terminated
                # and the global parser slot is yielded to the newer upload.
                return row['generation'] != -1
            session = db.execute("SELECT generation FROM sessions WHERE id=? AND state='active'", (row['document'],)).fetchone()
            return session is not None and session['generation'] == row['generation']

    def revoke(self, lease):
        """Immediately invalidate one non-upload lease.

        Setting the lease generation to -1 makes valid_lease return False on the
        supervisor's next poll even though the session generation (which any
        in-flight tile render depends on) stays unchanged. Upload leases are
        left alone: they are valid until their own lifecycle ends, and a missing
        lease is silently ignored.
        """
        with self.transaction() as db:
            db.execute("UPDATE leases SET generation=-1 WHERE id=? AND kind NOT LIKE 'upload%'", (lease,))

    def release(self, lease):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM leases WHERE id=?', (lease,)).fetchone()
            if row is None:
                return
            if process_alive(row['pid'], row['identity']):
                return  # Unknown/living child cannot free its FD/slot budget.
            folder = self.root / ('staging' if row['kind'].startswith('upload') else 'operations') / lease
            shutil.rmtree(folder, ignore_errors=False) if folder.exists() else None
            db.execute('DELETE FROM leases WHERE id=?', (lease,))
            self._expire(db)

    def metadata_path(self,document,version):
        return self.root/'metadata'/(document+'.'+version+'.json')

    def read_metadata(self,row):
        path=self.root/'metadata'/row['meta']
        raw=path.read_bytes()
        if len(raw)>META_BOUND:
            raise ProjectionRejected('projection_metadata_budget','投影元数据超过预算',503)
        # expires in SQLite is authoritative; metadata version need not change
        # on heartbeat. A GET must never change its expiry.
        return {**json.loads(raw),'expires_at':row['expires']}

    def _write_metadata(self,path,meta):
        raw=json.dumps({k:v for k,v in meta.items() if not k.startswith('_')},allow_nan=False).encode()
        if len(raw)>META_BOUND:
            raise ProjectionRejected('projection_metadata_budget','投影元数据超过文件预算',503)
        with path.open('xb') as file:
            file.write(raw);file.flush();os.fsync(file.fileno())

    def complete_upload(self, lease, owner, source, meta, discarded=None):
        """Promote a parsed upload to a live session.

        ``discarded`` marks a real client upload: the caller passes a list that
        receives the analysis_id of every other active document this upload
        retires, so the route can drop their queued analysis jobs. Test helpers
        that write sessions directly omit it and keep the previous semantics.
        """
        document = secrets.token_hex(32)
        version=secrets.token_hex(8)
        metadata=self.metadata_path(document,version)
        target=self.source_path(document)
        pending=[]
        try:
            with self.transaction() as db:
                row = db.execute('SELECT * FROM leases WHERE id=? AND owner=?', (lease, owner)).fetchone()
                if row is None:
                    raise ProjectionRejected('projection_upload_lost', '投影上传状态已失效', 503)
                if discarded is not None:
                    # Last initiated upload wins. A newer in-flight upload, or a
                    # session reserved after this lease, supersedes it before any
                    # bytes become visible.
                    self._reject_superseded(db, owner, lease, row['created'], row['generation'])
                size = Path(source).stat().st_size
                derived = meta.get('source_format') == 'plt'
                limit = row['derived_budget'] if derived else MAX_SOURCE
                if size > limit:
                    raise ProjectionRejected('projection_derived_too_large' if derived else 'projection_source_too_large',
                        'PLT 投影派生 PDF 超过文件预算' if derived else '上传文件不能超过20MB', 413)

                meta = {**meta, 'reserved_at': row['created'], 'reserved_by': lease}
                self._write_metadata(metadata,meta)
                os.replace(source, target)
                db.execute('INSERT INTO sessions(id,owner,expires,generation,state,source_bytes,meta,budget,derived_bytes) VALUES(?,?,?,0,?,?,?,?,?)',
                           (document, owner, meta['expires_at'], 'active', 0 if derived else size, metadata.name, allocation(size)+2*META_BOUND, size if derived else 0))
                db.execute("UPDATE leases SET kind='upload_done',budget=?,source_budget=0,derived_budget=0 WHERE id=?",(2*META_BOUND+65536,lease))
                if discarded is not None:
                    # Retire every other live document in the same transaction so
                    # a racing upload can never leave two active sessions.
                    pending = self._retire_others(db, owner, document, discarded)
        except BaseException:
            # If process crashes instead, its lease stays durable; reconcile
            # checks process identity and removes these unreferenced files.
            target.unlink(missing_ok=True);metadata.unlink(missing_ok=True)
            raise
        for retired in pending:
            self.release(retired)
        return document

    def _session_reservation(self, meta_name):
        """Return (reserved_at, reserved_by) recorded when a session was created."""
        try:
            data = json.loads((self.root / 'metadata' / meta_name).read_bytes())
        except (OSError, ValueError):
            return None
        reserved_at = data.get('reserved_at')
        if isinstance(reserved_at, bool) or not isinstance(reserved_at, (int, float)):
            return None
        reserved_by = data.get('reserved_by')
        return reserved_at, reserved_by if isinstance(reserved_by, str) else None

    def upload_superseded(self, lease):
        """True when an upload lease is gone or was replaced by a newer upload."""
        with self.transaction() as db:
            row = db.execute('SELECT generation FROM leases WHERE id=?', (lease,)).fetchone()
            return row is None or row['generation'] == -1

    def _reject_superseded(self, db, owner, lease, created, generation=None):
        if generation == -1:
            # reserve() already marked this upload superseded; it must never
            # become the online document.
            raise ProjectionRejected('projection_upload_superseded','已选择更新的纸样，本次上传已取消',409,0)
        newer = db.execute(
            "SELECT 1 FROM leases WHERE owner=? AND kind IN ('upload','upload-parse') AND id!=? "
            "AND (created>? OR (created=? AND id>?)) LIMIT 1",
            (owner, lease, created, created, lease)).fetchone()
        superseded = newer is not None
        if not superseded:
            for row in db.execute("SELECT meta FROM sessions WHERE owner=? AND state='active'", (owner,)).fetchall():
                reserved = self._session_reservation(row['meta'])
                if reserved is None:
                    continue
                reserved_at, reserved_by = reserved
                # Equal timestamps fall back to lease id ordering so the loser
                # is deterministic rather than whichever commit happens last.
                if reserved_at > created or (reserved_at == created and reserved_by is not None and reserved_by > lease):
                    superseded = True
                    break
        if superseded:
            raise ProjectionRejected('projection_upload_superseded','已选择更新的纸样，本次上传已取消',409,0)

    def _retire_others(self, db, owner, document, discarded):
        pending=[]
        for row in db.execute("SELECT id,meta FROM sessions WHERE owner=? AND state='active' AND id!=?",
                              (owner, document)).fetchall():
            analysis_id = self._analyzing_analysis_id(row['meta'])
            if analysis_id:
                discarded.append(analysis_id)
            db.execute("UPDATE sessions SET state='deleted',generation=generation+1 WHERE id=?", (row['id'],))
            pending.extend(r['id'] for r in db.execute('SELECT id FROM leases WHERE document=? AND pid IS NULL',(row['id'],)))
        self._expire(db)
        return pending

    def _load(self, db, document, owner):
        self._expire(db)
        return db.execute("SELECT * FROM sessions WHERE id=? AND owner=? AND state='active'", (document, owner)).fetchone()

    def load(self, document, owner):
        with self.transaction() as db:
            row = self._load(db, document, owner)
            if row is None:
                return None
            return {**self.read_metadata(row), '_generation': row['generation']}

    def renew(self, document, owner):
        with self.transaction() as db:
            row = self._load(db, document, owner)
            if row is None:
                return False
            db.execute('UPDATE sessions SET expires=? WHERE id=?', (time.time()+3600,document))
        return True

    def delete(self, document, owner):
        pending = []
        with self.transaction() as db:
            row = self._load(db, document, owner)
            if row is None:
                return False
            db.execute("UPDATE sessions SET state='deleted',generation=generation+1 WHERE id=?", (document,))
            pending = [r['id'] for r in db.execute('SELECT id FROM leases WHERE document=? AND pid IS NULL',(document,))]
            self._expire(db)
        for lease in pending:
            self.release(lease)
        return True

    def _analyzing_analysis_id(self, meta_name):
        try:
            data = json.loads((self.root / 'metadata' / meta_name).read_bytes())
        except (OSError, ValueError):
            return None
        analysis = data.get('analysis') or {}
        return analysis.get('analysis_id') if analysis.get('status') == 'analyzing' else None

    def delete_all(self, owner):
        """Retire every active projection document for an owner.

        Product rule: one live pattern per account; a new upload replaces the
        previous one. Reuse the per-document deletion path so idle leases,
        files and ledger budget are reclaimed immediately while in-flight work
        only loses its generation (it fails on valid_lease, never with 500).
        Return the analysis_id of every retired document that still had a
        queued analysis, so the caller can remove those jobs from the queue.
        """
        pending = []
        with self.transaction() as db:
            self._expire(db)
            rows = [dict(r) for r in db.execute("SELECT id,meta FROM sessions WHERE owner=? AND state='active'", (owner,))]
            analysis_ids = [aid for aid in (self._analyzing_analysis_id(row['meta']) for row in rows) if aid]
            for row in rows:
                db.execute("UPDATE sessions SET state='deleted',generation=generation+1 WHERE id=?", (row['id'],))
                pending.extend(r['id'] for r in db.execute('SELECT id FROM leases WHERE document=? AND pid IS NULL',(row['id'],)))
            self._expire(db)
        for lease in pending:
            self.release(lease)
        return analysis_ids

    def analyzing_documents(self, owner, exclude=None):
        """Documents for one owner currently reporting an in-flight analysis."""
        with self.transaction() as db:
            rows = [dict(r) for r in db.execute("SELECT id,meta FROM sessions WHERE owner=? AND state='active'", (owner,))]
        analyzing = []
        for row in rows:
            if row['id'] == exclude:
                continue
            try:
                meta = json.loads((self.root / 'metadata' / row['meta']).read_bytes())
            except (OSError, ValueError):
                continue
            if (meta.get('analysis') or {}).get('status') == 'analyzing':
                analyzing.append(row['id'])
        return analyzing

    def update_analysis(self,document,owner,analysis_id,generation,change,invalidate=False):
        # CAS only the analysis state. Phase polling must not invalidate an
        # otherwise legitimate worker result or overwrite unrelated metadata.
        return self.update(document,owner,None,generation,invalidate,
                           analysis_guard=(analysis_id,change))

    def replace_analysis(self,document,owner,analysis,generation,expected_analysis_id):
        # Retiring the old child must commit with the metadata replacement. A
        # supervisor crash after this transaction cannot leave its lease valid.
        return self.update(document,owner,None,generation,
                           analysis_replacement=(expected_analysis_id,analysis))

    def update(self, document, owner, meta, generation, invalidate=False, analysis_guard=None,
               analysis_replacement=None):
        new=self.metadata_path(document,secrets.token_hex(8));old=None
        try:
            with self.transaction() as db:
                row = self._load(db, document, owner)
                if row is None or row['generation'] != generation:
                    return False
                if analysis_replacement is not None:
                    expected_analysis_id,analysis=analysis_replacement
                    latest=self.read_metadata(row)
                    current=latest.get('analysis') or {}
                    if current.get('analysis_id')!=expected_analysis_id:
                        return False
                    prior_lease=current.get('lease')
                    latest['analysis']=analysis
                    meta=latest
                    if prior_lease and prior_lease!=analysis.get('lease'):
                        db.execute("UPDATE leases SET generation=-1 WHERE id=? AND owner=? AND document=? AND kind='analysis'",
                                   (prior_lease,owner,document))
                if analysis_guard is not None:
                    analysis_id,change=analysis_guard
                    latest=self.read_metadata(row)
                    current=latest.get('analysis') or {}
                    if current.get('analysis_id')!=analysis_id or current.get('status')!='analyzing':
                        return False
                    # An old queued observation cannot regress started work.
                    if change.get('phase')=='queued' and current.get('phase')=='analyzing':
                        return False
                    latest['analysis']={**current,**change}
                    meta=latest
                old=self.root/'metadata'/row['meta']
                # Two metadata versions are reserved for each session. Reclaim
                # failed/unreferenced prior versions before writing a third.
                for file in (self.root/'metadata').glob(document+'.*.json'):
                    if file != old:file.unlink(missing_ok=True)
                clean={k:v for k,v in meta.items() if not k.startswith('_')}
                clean['expires_at']=row['expires']
                self._write_metadata(new,clean)
                db.execute('UPDATE sessions SET meta=?,generation=generation+? WHERE id=?',
                           (new.name,int(invalidate),document))
        except BaseException:
            new.unlink(missing_ok=True)
            raise
        # Failure to unlink old version retains the full two-copy reservation.
        # The next write must successfully clean it before proceeding.
        try:old.unlink(missing_ok=True)
        except OSError:pass
        return True

    def reconcile(self):
        # Reconciliation never trusts age alone to release a process lease. A
        # still-living supervisor or child keeps its reservation; Linux child
        # bootstrap handles parent death, then the next reconcile reclaims it.
        with self.transaction() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM leases')]
        for row in rows:
            if row['kind']=='analysis' and row['pid'] is None and row['created']+3600<=time.time():
                self.release(row['id'])
                continue
            if row['pid'] == -1 and not process_alive(row['supervisor'],row['supervisor_identity']):
                folder=self.root/('staging' if row['kind'].startswith('upload') else 'operations')/row['id']
                try:
                    marker=json.loads((folder/'child.json').read_text())
                except (OSError,ValueError):
                    marker={'pid':None,'identity':None}
                if process_alive(marker['pid'],marker['identity']):
                    continue
                with self.transaction() as db:
                    db.execute('UPDATE leases SET pid=NULL,identity=NULL WHERE id=?',(row['id'],))
                row['pid']=None
            if not process_alive(row['supervisor'], row['supervisor_identity']) and not process_alive(row['pid'], row['identity']):
                self.release(row['id'])
        with self.transaction() as db:
            self._expire(db)
            sessions=db.execute('SELECT id,meta FROM sessions').fetchall()
            expected = {r['id'] for r in sessions}
            metadata={r['meta'] for r in sessions}
            for path in (self.root/'metadata').glob('*.json'):
                if path.name not in metadata:path.unlink(missing_ok=True)
            for path in (self.root/'documents').glob('*.pdf'):
                if path.stem not in expected:
                    path.unlink(missing_ok=True)
            # A reserve->claim race can leave a fully-named lease directory that
            # never got its ledger row (or lost it); those leftovers would be
            # charged to the disk budget forever. Remove only 64-hex directories
            # that no live lease references; a failed unlink keeps the bytes.
            lease_ids = {row['id'] for row in db.execute('SELECT id FROM leases')}
            for folder in ('operations', 'staging'):
                base = self.root / folder
                if not base.is_dir():
                    continue
                for path in base.iterdir():
                    if path.is_dir() and path.name not in lease_ids and re.fullmatch(r'[0-9a-f]{64}', path.name):
                        shutil.rmtree(path, ignore_errors=True)
