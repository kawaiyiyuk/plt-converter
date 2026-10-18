"""Immutable scene bundles share the projection ledger and lifecycle.

Only supervised children export PDF content. This module checks and moves bytes;
no PDF library is loaded by the HTTP or download path. Download leases retain
both the source session and ZIP charge until their file descriptor is closed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
import zipfile

# Bump whenever extraction, compositing, geometry or asset sampling semantics
# change, even if scene JSON stays version 1. Kept here so static HTTP/download
# paths do not import the native exporter. Older pipelines remain charged until
# their last open download closes; they can never satisfy a current cache hit.
SCENE_PIPELINE_VERSION = 'scene-pipeline-v2-gutter2-hairline'

CHUNK = 64 * 1024
ASSET = re.compile(r'^assets/([0-9a-f]{64})\.png$')


def rejected(code, message, status=503):
    from .projection_store import ProjectionRejected
    return ProjectionRejected(code, message, status, 1 if status in (429,503) else 0)


def validate_bundle(path, maximum):
    """Validate ZIP CRC, names, hashes and independent compressed/unpacked caps.

    Stream decompressed members into a digest; never materialize the archive or
    an unpacked tree in the server process. The supervised producer is trusted
    code, but a partial/corrupt artifact must never be published as successful.
    """
    size = path.stat().st_size
    if not 0 < size <= maximum:
        raise rejected('projection_scene_budget', '本地投影素材超过派生文件预算', 400)
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''):
            digest.update(chunk)
    unpacked = 0
    try:
        with zipfile.ZipFile(path) as archive:
            names = set()
            for member in archive.infolist():
                asset = ASSET.fullmatch(member.filename)
                if (member.filename in names or (member.filename != 'scene.json' and not asset)
                        or member.is_dir() or member.flag_bits & 1):
                    raise ValueError('unsafe scene member')
                names.add(member.filename)
                if member.file_size < 0 or unpacked + member.file_size > maximum:
                    raise rejected('projection_scene_budget', '本地投影素材展开后超过派生文件预算', 400)
                counted = 0
                checksum = hashlib.sha256()
                with archive.open(member) as source:
                    for chunk in iter(lambda: source.read(CHUNK), b''):
                        counted += len(chunk)
                        if unpacked + counted > maximum:
                            raise rejected('projection_scene_budget', '本地投影素材展开后超过派生文件预算', 400)
                        checksum.update(chunk)
                if counted != member.file_size or (asset and checksum.hexdigest() != asset[1]):
                    raise ValueError('scene asset hash mismatch')
                unpacked += counted
            if 'scene.json' not in names:
                raise ValueError('missing scene.json')
    except (ValueError, zipfile.BadZipFile, RuntimeError) as error:
        raise rejected('projection_scene_corrupt', '本地投影素材不完整，请重新选择文件') from error
    return {'sha256': digest.hexdigest(), 'bytes': size, 'unpacked_bytes': unpacked}


class SceneStoreMixin:
    def initialize_scenes(self, db):
        schema = '''CREATE TABLE IF NOT EXISTS scene_bundles(
          document TEXT NOT NULL, generation INTEGER NOT NULL, variant TEXT NOT NULL,
          state TEXT NOT NULL, revision TEXT, lease TEXT, descriptor TEXT,
          budget INTEGER NOT NULL DEFAULT 0, derived_bytes INTEGER NOT NULL DEFAULT 0,
          pipeline TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(document,generation,variant,pipeline))'''
        db.execute(schema)
        if 'pipeline' not in {row['name'] for row in db.execute('PRAGMA table_info(scene_bundles)')}:
            # SQLite cannot extend a primary key with ALTER COLUMN. Rebuild in
            # the caller's IMMEDIATE transaction, copying every charged row.
            # Legacy rows deliberately keep pipeline='' rather than claiming
            # they were produced by the current extractor. Download leases are
            # keyed by revision and remain untouched throughout the migration.
            db.execute('ALTER TABLE scene_bundles RENAME TO scene_bundles_pre_pipeline')
            db.execute(schema)
            db.execute('''INSERT INTO scene_bundles(document,generation,variant,state,revision,lease,descriptor,budget,derived_bytes)
              SELECT document,generation,variant,state,revision,lease,descriptor,budget,derived_bytes
              FROM scene_bundles_pre_pipeline''')
            db.execute('DROP TABLE scene_bundles_pre_pipeline')
        if 'scene_revision' not in {row['name'] for row in db.execute('PRAGMA table_info(leases)')}:
            db.execute('ALTER TABLE leases ADD COLUMN scene_revision TEXT')
        # Earlier local candidates had a one-bundle table. Migrate additively,
        # once, without dropping an existing user's session/ledger or files.
        db.execute('CREATE TABLE IF NOT EXISTS scene_schema_migrations(name TEXT PRIMARY KEY)')
        if not db.execute("SELECT 1 FROM scene_schema_migrations WHERE name='variants-v1'").fetchone():
            legacy=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenes'").fetchone()
            if legacy:
                for row in db.execute('SELECT * FROM scenes').fetchall():
                    session=db.execute('SELECT * FROM sessions WHERE id=?',(row['document'],)).fetchone()
                    meta=self.read_metadata(session) if session else {}
                    view='layout' if meta.get('layout') else 'page'
                    variant='layout' if view=='layout' else 'page:0'
                    descriptor=json.loads(row['descriptor']) if row['descriptor'] else None
                    if descriptor:
                        descriptor['view']=view
                        if view=='page':descriptor['page_index']=0
                    db.execute('''INSERT OR IGNORE INTO scene_bundles(document,generation,variant,state,
                      revision,lease,descriptor,budget,derived_bytes) VALUES(?,?,?,?,?,?,?,?,?)''',
                      (row['document'],row['generation'],variant,row['state'],row['revision'],row['lease'],
                       json.dumps(descriptor) if descriptor else None,row['budget'],row['derived_bytes']))
                    if row['revision']:
                        db.execute("UPDATE leases SET scene_revision=? WHERE document=? AND generation=? AND kind='scene-download' AND scene_revision IS NULL",
                                   (row['revision'],row['document'],row['generation']))
            db.execute("INSERT INTO scene_schema_migrations VALUES('variants-v1')")

    def scene_path(self, document, revision):
        return self.root / 'documents' / (document + '.' + revision + '.zip')

    def expire_scenes(self, db):
        # A downloader holds the original generation in its lease. Even after a
        # layout/delete invalidates it, its open FD and charge remain together.
        rows = db.execute('''SELECT scene_bundles.* FROM scene_bundles LEFT JOIN sessions ON sessions.id=scene_bundles.document
          WHERE sessions.id IS NULL OR sessions.state!='active' OR scene_bundles.generation!=sessions.generation
          OR scene_bundles.pipeline!=?
          OR (scene_bundles.state='pending' AND NOT EXISTS(SELECT 1 FROM leases WHERE id=scene_bundles.lease))''',
          (SCENE_PIPELINE_VERSION,)).fetchall()
        for row in rows:
            if db.execute("SELECT 1 FROM leases WHERE document=? AND scene_revision=? AND kind='scene-download'",
                          (row['document'],row['revision'])).fetchone():
                continue
            if row['revision']:
                try:
                    self.scene_path(row['document'],row['revision']).unlink(missing_ok=True)
                except OSError:
                    continue
            db.execute('UPDATE sessions SET budget=budget-?,derived_bytes=derived_bytes-? WHERE id=?',
                       (row['budget'],row['derived_bytes'],row['document']))
            db.execute('DELETE FROM scene_bundles WHERE document=? AND generation=? AND variant=? AND pipeline=?',
                       (row['document'],row['generation'],row['variant'],row['pipeline']))

    def _scene_slots(self, db, owner):
        kinds = "('tile','scene','scene-download')"
        total=db.execute('SELECT COUNT(*) FROM leases WHERE kind IN '+kinds).fetchone()[0]
        own=db.execute('SELECT COUNT(*) FROM leases WHERE kind IN '+kinds+' AND owner=?',(owner,)).fetchone()[0]
        if total >= self.policy.global_renderers or own >= self.policy.owner_renderers:
            raise rejected('projection_renderer_busy','投影素材处理或下载繁忙，请稍后重试',429)

    def scene_begin(self, document, owner, generation, variant):
        from .projection_store import allocation, process_identity
        with self.transaction() as db:
            session = self._load(db, document, owner)
            if session is None:
                raise rejected('projection_not_found','投影文件不存在或已过期',404)
            if session['generation'] != generation:
                raise rejected('projection_result_stale','投影排版已变化，请重新加载',409)
            scene = db.execute('SELECT * FROM scene_bundles WHERE document=? AND generation=? AND variant=? AND pipeline=?',(document,generation,variant,SCENE_PIPELINE_VERSION)).fetchone()
            if scene:
                if scene['state'] == 'invalid':
                    raise rejected('projection_scene_corrupt','本地投影素材不完整，请重新选择文件')
                return dict(scene), False
            maximum = self.policy.derived_max_bytes
            if not maximum or not self.policy.owner_derived_bytes:
                raise rejected('projection_scene_unconfigured','本地投影派生文件预算尚未配置')
            self._scene_slots(db,owner)
            used=db.execute('SELECT COALESCE(SUM(derived_bytes),0) FROM sessions WHERE owner=?',(owner,)).fetchone()[0]
            pending=db.execute('SELECT COALESCE(SUM(derived_budget),0) FROM leases WHERE owner=?',(owner,)).fetchone()[0]
            if used + pending + maximum > self.policy.owner_derived_bytes:
                raise rejected('projection_owner_derived_capacity','投影临时派生文件额度已满，请稍后重试',429)
            # ZIP is the only durable output; unpacked members are streamed in
            # memory. Reserve disk for the ZIP and the existing control budget.
            budget = self.policy.operation_bytes + allocation(maximum)
            self._capacity(db,budget,'scene')
            lease=secrets.token_hex(32)
            db.execute('''INSERT INTO leases(id,owner,kind,document,generation,budget,source_budget,pid,identity,
              supervisor,supervisor_identity,created,derived_budget) VALUES(?,?,'scene',?,?,?,0,NULL,NULL,?,?,?,?)''',
              (lease,owner,document,generation,budget,os.getpid(),process_identity(os.getpid()),time.time(),maximum))
            db.execute("INSERT INTO scene_bundles(document,generation,variant,pipeline,state,lease) VALUES(?,?,?,?,'pending',?)",(document,generation,variant,SCENE_PIPELINE_VERSION,lease))
            return {'document':document,'generation':generation,'variant':variant,'pipeline':SCENE_PIPELINE_VERSION,'state':'pending','lease':lease}, True

    def scene_snapshot(self, document, owner, generation, variant):
        with self.transaction() as db:
            row=self._load(db,document,owner)
            if row is None:
                raise rejected('projection_not_found','投影文件不存在或已过期',404)
            if row['generation'] != generation:
                raise rejected('projection_result_stale','投影排版已变化，请重新加载',409)
            scene=db.execute('SELECT * FROM scene_bundles WHERE document=? AND generation=? AND variant=? AND pipeline=?',(document,generation,variant,SCENE_PIPELINE_VERSION)).fetchone()
            if scene is None:
                raise rejected('projection_scene_failed','本地投影素材准备失败，请重试')
            if scene['state']=='invalid':
                raise rejected('projection_scene_corrupt','本地投影素材不完整，请重新选择文件')
            return dict(scene)

    def scene_publish(self, document, owner, generation, variant, lease, meta, layout, result):
        from .projection_store import allocation
        bundle=self.lease_path(lease)/'scene.zip'
        checked=validate_bundle(bundle,self.policy.derived_max_bytes)
        if any(result.get(k)!=v for k,v in checked.items()):
            raise rejected('projection_scene_corrupt','本地投影素材校验失败，请重新选择文件')
        source_sha=result.get('source_sha256')
        if not isinstance(source_sha,str) or not re.fullmatch('[0-9a-f]{64}',source_sha):
            raise rejected('projection_scene_corrupt','本地投影素材源文件校验失败')
        binding=json.dumps([SCENE_PIPELINE_VERSION,document,generation,variant,source_sha,layout],sort_keys=True,separators=(',',':'),allow_nan=False)
        revision=hashlib.sha256(binding.encode()).hexdigest()
        descriptor={'document_id':document,'revision':revision,'source_format':meta.get('source_format','pdf'),
                    'width_mm':layout['width_mm'],'height_mm':layout['height_mm'],
                    'view':'layout' if variant=='layout' else 'page',
                    **({'page_index':int(variant.split(':')[1])} if variant.startswith('page:') else {}),
                    'cache_owner':hashlib.sha256(owner.encode()).hexdigest(),
                    'bundle':{'path':f'/api/v1/pdf/projection/documents/{document}/scene/{revision}/bundle',**checked}}
        target=self.scene_path(document,revision)
        moved=False
        try:
            with self.transaction() as db:
                row=self._load(db,document,owner)
                active=db.execute("SELECT 1 FROM scene_bundles WHERE document=? AND generation=? AND variant=? AND pipeline=? AND state='pending' AND lease=?",(document,generation,variant,SCENE_PIPELINE_VERSION,lease)).fetchone()
                admitted=db.execute('SELECT budget FROM leases WHERE id=? AND generation=?',(lease,generation)).fetchone()
                if row is None or row['generation']!=generation or not active or not admitted:
                    raise rejected('projection_result_stale','投影会话或排版已变化，请重新加载',409)
                os.replace(bundle,target);moved=True
                charge=allocation(checked['bytes'])
                db.execute('UPDATE sessions SET budget=budget+?,derived_bytes=derived_bytes+? WHERE id=?',(charge,checked['bytes'],document))
                db.execute("UPDATE scene_bundles SET state='ready',revision=?,descriptor=?,budget=?,derived_bytes=? WHERE document=? AND generation=? AND variant=? AND pipeline=? AND lease=?",(revision,json.dumps(descriptor,allow_nan=False),charge,checked['bytes'],document,generation,variant,SCENE_PIPELINE_VERSION,lease))
                # Transfer the output reservation to the durable session exactly
                # once; control files remain owned by the still-live lease.
                db.execute('UPDATE leases SET budget=?,derived_budget=0 WHERE id=?',(self.policy.operation_bytes,lease))
        except BaseException:
            if moved:target.unlink(missing_ok=True)
            raise
        return descriptor

    def scene_failed(self, document, generation, variant, lease):
        with self.transaction() as db:
            db.execute("DELETE FROM scene_bundles WHERE document=? AND generation=? AND variant=? AND state='pending' AND lease=?",(document,generation,variant,lease))

    def scene_descriptor(self, document, owner, scene):
        meta=self.load(document,owner)
        if meta is None or meta['_generation']!=scene['generation'] or scene.get('pipeline')!=SCENE_PIPELINE_VERSION:
            raise rejected('projection_result_stale','投影会话或排版已变化，请重新加载',409)
        # A descriptor must not claim a missing/partial/corrupt cache is ready.
        # Verify through a bounded FD lease as well; this performs only static
        # streaming reads and never calls a PDF library or supervised exporter.
        file,lease,_=self.scene_open(document,owner,scene['revision'])
        file.close()
        self.scene_close(lease)
        descriptor=json.loads(scene['descriptor'])
        descriptor['expires_in']=max(0,__import__('math').ceil(meta['expires_at']-time.time()))
        return descriptor

    def scene_open(self, document, owner, revision):
        from .projection_store import process_identity
        # Open and create a download lease atomically with the final
        # owner/generation guard. No delete can unlink an unaccounted open FD.
        file=None
        try:
            with self.transaction() as db:
                session=self._load(db,document,owner)
                scene=db.execute("SELECT * FROM scene_bundles WHERE document=? AND revision=? AND pipeline=? AND state='ready'",(document,revision,SCENE_PIPELINE_VERSION)).fetchone()
                if session is None or scene is None or session['generation']!=scene['generation']:
                    raise rejected('projection_not_found','投影文件不存在或已过期',404)
                self._scene_slots(db,owner)
                file=self.scene_path(document,revision).open('rb')
                lease=secrets.token_hex(32)
                # Only the operation directory/control row is new disk; the
                # opened ZIP stays charged to the original scene/session.
                budget=self.policy.operation_bytes
                self._capacity(db,budget,'scene-download')
                db.execute('''INSERT INTO leases(id,owner,kind,document,generation,budget,source_budget,pid,identity,supervisor,supervisor_identity,created,scene_revision)
                  VALUES(?,?,'scene-download',?,?,?,0,?,?,?,?,?,?)''',
                  (lease,owner,document,scene['generation'],budget,os.getpid(),process_identity(os.getpid()),os.getpid(),process_identity(os.getpid()),time.time(),revision))
            descriptor=json.loads(scene['descriptor'])
            digest=hashlib.sha256();counted=0
            for chunk in iter(lambda:file.read(CHUNK),b''):
                digest.update(chunk);counted+=len(chunk)
            if counted!=descriptor['bundle']['bytes'] or digest.hexdigest()!=descriptor['bundle']['sha256']:
                raise rejected('projection_scene_corrupt','本地投影素材不完整，请重新选择文件')
            file.seek(0)
            if not self.valid_lease(lease):
                raise rejected('projection_result_stale','投影会话或排版已变化，请重新加载',409)
            return file,lease,descriptor['bundle']
        except BaseException:
            if file is not None:file.close()
            if 'lease' in locals():self.scene_close(lease)
            raise

    def scene_close(self, lease):
        # Caller MUST close its FD first. Clearing pid then releasing makes
        # orphan cleanup use the same accounting path as supervised children.
        with self.transaction() as db:
            db.execute("UPDATE leases SET pid=NULL,identity=NULL WHERE id=? AND kind='scene-download'",(lease,))
        self.release(lease)
