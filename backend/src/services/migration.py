import os,io,json,zipfile,hashlib,logging,threading
from sqlalchemy import select,text,func
from fastapi import HTTPException
from src.config.database import Session
from src.models.study import Study
from src.models.observation import Observation
from src.models.run import Run
from src.models.review import Review
from src.models.import_batch import ImportBatch
from src.services.audits import log
from src.utils.serializers import record,now

PACKAGE_FORMAT=1
MAX_PACKAGE_BYTES=256*1024*1024
REQUIRED_ENTITIES=('study','observations','runs','reviews','artifacts')

def station_id():
    # Stable local station identity; overridden per deployment in .env.
    return os.environ.get('STATION_ID','station-local-default')

def artifact_root():
    return os.environ.get('ARTIFACT_DIR','/app/artifacts')

def _sha256_bytes(b): return hashlib.sha256(b).hexdigest()

# --------------------------------------------------------------------------- #
# Export: one point in time, read-only; frozen inputs / analysis / reviews and
# calculation artifacts travel together, local registrations stay editable.
# --------------------------------------------------------------------------- #
def build_package(db,study_id,user):
    study=db.get(Study,study_id)
    if not study: raise HTTPException(404,'业务批次不存在')
    runs=list(db.scalars(select(Run).where(Run.study_id==study_id).order_by(Run.id)))
    run_ids={r.id for r in runs}
    observations=[record(x) for x in db.scalars(
        select(Observation).where(Observation.study_id==study_id).order_by(Observation.id))]
    reviews=[record(x) for x in db.scalars(select(Review).order_by(Review.id))
             if x.run_id in run_ids]
    artifacts=[]
    for r in runs:
        path=os.path.join(artifact_root(),f'run-{r.id}.json')
        if os.path.exists(path):
            with open(path,'rb') as fh: content=fh.read()
            artifacts.append({'run_id':r.id,'file':f'artifacts/run-{r.id}.json',
                              'sha256':_sha256_bytes(content),'size':len(content)})
    manifest={
        'format':PACKAGE_FORMAT,
        'station_id':station_id(),
        'exported_at':now(),
        'study':record(study),
        'observations':observations,
        'runs':[record(r) for r in runs],
        'reviews':reviews,
        'artifacts':artifacts,
    }
    # Checksum binds the manifest: any content change after export is detectable.
    manifest['package_checksum']=checksum_json(manifest)
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,allow_nan=False,sort_keys=True))
        for item in artifacts:
            path=os.path.join(artifact_root(),f"run-{item['run_id']}.json")
            zf.write(path,item['file'])
    log(db,user,'package.export',f'study {study_id} @ {manifest["exported_at"]} / {manifest["package_checksum"][:12]}')
    db.commit()
    return buffer.getvalue(),manifest

def checksum_json(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

# --------------------------------------------------------------------------- #
# Import: verify references and artifact checksums, map ids by source, keep
# same-name different-source studies side by side, stay idempotent for the same
# source, reject changed content, and publish atomically.
# --------------------------------------------------------------------------- #
def _verify_package(raw):
    if len(raw)>MAX_PACKAGE_BYTES: raise HTTPException(422,'研究包超过256MB上限')
    try:
        zf=zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(422,'坏包：无法解压的研究包文件') from e
    if 'manifest.json' not in zf.namelist(): raise HTTPException(422,'坏包：缺少 manifest.json')
    for name in zf.namelist():  # block absolute paths and traversal entries
        if name.endswith('/'): continue
        if name.startswith('/') or '..' in name.split('/') or os.path.isabs(name):
            raise HTTPException(422,'坏包：包含非法归档路径')
    try:
        manifest=json.loads(zf.read('manifest.json').decode('utf-8'))
    except Exception as e:
        raise HTTPException(422,'坏包：清单无法解析') from e
    claimed=manifest.pop('package_checksum',None)
    if not claimed or not all(k in manifest for k in REQUIRED_ENTITIES) or manifest.get('format')!=PACKAGE_FORMAT:
        raise HTTPException(422,'坏包：清单结构不完整或版本不受支持')
    if checksum_json(manifest)!=claimed: raise HTTPException(422,'研究包内容已变动，校验拒绝')
    _cross_check(manifest,zf)
    return manifest,claimed

def _cross_check(manifest,zf):
    study=manifest['study']
    if not isinstance(study,dict) or 'id' not in study: raise HTTPException(422,'坏包：缺少来源研究')
    source_station=str(manifest.get('station_id') or '')
    if not source_station: raise HTTPException(422,'坏包：缺少来源站点标识')
    if source_station==station_id(): raise HTTPException(422,'本站点导出的研究包不可迁入本站')
    run_ids=set()
    for r in manifest['runs']:
        if r.get('study_id')!=study['id']: raise HTTPException(422,'引用核验失败：运行引用了包外研究')
        if checksum_json(r.get('snapshot'))!=r.get('checksum'): raise HTTPException(422,'留档校验失败：冻结输入与SHA256不一致')
        if r['id'] in run_ids: raise HTTPException(422,'坏包：运行编号重复')
        run_ids.add(r['id'])
    for o in manifest['observations']:
        if o.get('study_id')!=study['id']: raise HTTPException(422,'引用核验失败：纸样引用了包外研究')
    keys=[o.get('source_key') for o in manifest['observations']]
    if len(keys)!=len(set(keys)): raise HTTPException(422,'坏包：来源纸样编号在包内重复')
    for rv in manifest['reviews']:
        if rv.get('run_id') not in run_ids: raise HTTPException(422,'引用核验失败：复核引用了包外运行')
    # Artifact archive: every listed file exists, hashes match, and its
    # embedded input checksum belongs to the run it claims.
    seen=set()
    for item in manifest['artifacts']:
        if item['run_id'] not in run_ids: raise HTTPException(422,'引用核验失败：留档引用了包外运行')
        if item['file'] in seen: raise HTTPException(422,'坏包：留档条目重复')
        seen.add(item['file'])
        try: content=zf.read(item['file'])
        except KeyError as e: raise HTTPException(422,'留档校验失败：缺少计算留档文件') from e
        if _sha256_bytes(content)!=item['sha256']: raise HTTPException(422,'留档校验失败：留档SHA256不匹配')
        try: payload=json.loads(content.decode('utf-8'))
        except Exception as e: raise HTTPException(422,'留档校验失败：留档内容无法解析') from e
        run=next(r for r in manifest['runs'] if r['id']==item['run_id'])
        if payload.get('input_sha256')!=run['checksum']: raise HTTPException(422,'留档校验失败：留档与运行输入不匹配')

def _is_postgres(db): return db.bind.dialect.name=='postgresql'

def _next_id(db,model,highwater):
    table=model.__tablename__
    if _is_postgres(db):
        value=db.execute(text(f"SELECT nextval(pg_get_serial_sequence('{table}','id'))")).scalar()
        return int(value)
    # SQLite has no sequences: keep a Python high-water mark for the staging
    # session, since pre-allocated ids are not inserted rows yet.
    if table not in highwater:
        highwater[table]=int(db.execute(text(f'SELECT COALESCE(MAX(id),0) FROM {table}')).scalar() or 0)
    highwater[table]+=1
    return highwater[table]

def _normalize_mapping(mapping):
    # JSON round-trips dict keys as strings; restore integer source ids so a
    # retry in a new process reuses the exact same mapping.
    if not mapping: return {}
    return {kind:( {int(k):v for k,v in entries.items()} if isinstance(entries,dict) else entries)
            for kind,entries in mapping.items()}

def _acquire(db,key_int):
    # Same package -> same advisory key, so concurrent identical imports
    # serialize and follow one mapping; other packages never block each other.
    if _is_postgres(db): db.execute(text('SELECT pg_advisory_lock(:k)'),{'k':key_int})
    else:
        # SQLite has no advisory locks: the local backend runs in one process,
        # so a per-package threading lock provides the same serialization.
        with _thread_locks_guard:
            lock=_thread_locks.setdefault(key_int,threading.Lock())
        lock.acquire()

def _release(db,key_int):
    if _is_postgres(db): db.execute(text('SELECT pg_advisory_unlock(:k)'),{'k':key_int})
    else:
        with _thread_locks_guard:
            lock=_thread_locks.get(key_int)
        if lock: lock.release()

_thread_locks={}
_thread_locks_guard=threading.Lock()

def import_package(raw,user):
    manifest,package_checksum=_verify_package(raw)
    source_station=str(manifest['station_id']);source_study_id=int(manifest['study']['id'])
    key_int=int(package_checksum[:15],16)-2**59
    with Session() as lock_db:
        _acquire(lock_db,key_int)
        try:
            existing=lock_db.scalar(select(ImportBatch).where(
                ImportBatch.source_station==source_station,
                ImportBatch.source_study_id==source_study_id))
            if existing and existing.status=='completed':
                # Same source already published: never create a second copy.
                return _batch_summary(existing,idempotent=True)
            if existing and existing.package_checksum!=package_checksum:
                # staging/failed attempt with different content: refuse and
                # leave every original item untouched.
                raise HTTPException(409,'同源研究包内容已变动，拒绝迁入；原有项目保持不变')
            # None / staging / failed with the identical checksum: publish (or
            # resume) under one lock and the stored mapping.
            return _publish(raw,manifest,package_checksum,source_station,source_study_id,user)
        finally:
            _release(lock_db,key_int)

def _publish(raw,manifest,package_checksum,source_station,source_study_id,user):
    # Phase 1: durable staging record + pre-allocated id mapping, committed on
    # its own. A crash here leaves nothing business-visible; a retry reuses it.
    with Session() as stage_db:
        batch=stage_db.scalar(select(ImportBatch).where(
            ImportBatch.source_station==source_station,
            ImportBatch.source_study_id==source_study_id))
        if batch is None:
            batch=ImportBatch(source_station=source_station,source_study_id=source_study_id,
                              package_checksum=package_checksum,status='staging',mapping={},
                              created_at=now())
            stage_db.add(batch);stage_db.flush()
        mapping=_normalize_mapping(batch.mapping)
        mapping.setdefault('study',{})
        highwater={}
        for kind,model in (('observation',Observation),('run',Run),('review',Review)):
            mapping.setdefault(kind,{})
            needed={'observation':[o['id'] for o in manifest['observations']],
                    'run':[r['id'] for r in manifest['runs']],
                    'review':[v['id'] for v in manifest['reviews']]}[kind]
            for old_id in needed:
                if old_id not in mapping[kind]: mapping[kind][old_id]=_next_id(stage_db,model,highwater)
        mapping.setdefault('artifact',{})
        for item in manifest['artifacts']:
            old=item['run_id']
            if old not in mapping['artifact']:
                mapping['artifact'][old]=mapping['run'][old]
        if source_study_id not in mapping['study']:
            mapping['study'][source_study_id]=_next_id(stage_db,Study,highwater)
        batch.mapping=mapping;stage_db.commit();batch_id=batch.id
    # Phase 2: all business rows plus artifact files in one transaction.
    # Nothing is visible until the final commit; rollback removes every row.
    with Session() as db:
        try:
            new_study_id=mapping['study'][source_study_id]
            already=db.get(Study,new_study_id)
            if already is not None:
                # Resume after a crash between the phase-2 commit and the
                # completion marker: rows already exist, so do not duplicate.
                have_obs=db.scalar(select(func.count()).select_from(Observation)
                                   .where(Observation.study_id==new_study_id))
                have_runs=db.scalar(select(func.count()).select_from(Run)
                                    .where(Run.study_id==new_study_id))
                if have_obs!=len(manifest['observations']) or have_runs!=len(manifest['runs']):
                    raise RuntimeError('已发布研究与映射不一致，拒绝继续以避免半项')
                conflict=db.get(ImportBatch,batch_id).conflict_note or ''
                requeue=[r2 for r2 in (mapping['run'].get(r['id']) for r in manifest['runs'])
                         if db.get(Run,r2) and db.get(Run,r2).status=='queued']
            else:
                name=manifest['study']['name'];conflict=''
                if db.scalar(select(Study).where(Study.name==name)):
                    # Same name, different source: coexist under a suffixed name;
                    # the mapping keeps the original source identity.
                    name=f"{name}〔{source_station}〕"
                    conflict=f'同名异源：原名称已存在，迁入名称追加来源后缀 → {name}'
                db.add(Study(id=new_study_id,name=name,
                             source_filename=manifest['study']['source_filename'],
                             description=manifest['study'].get('description','')))
                for o in manifest['observations']:
                    db.add(Observation(id=mapping['observation'][o['id']],study_id=new_study_id,
                                       source_key=o['source_key'],values=o['values']))
                requeue=[]
                for r in manifest['runs']:
                    new_run_id=mapping['run'][r['id']]
                    status=r['status']
                    if status=='running': status='queued'  # interrupted at source: re-run locally
                    db.add(Run(id=new_run_id,study_id=new_study_id,name=r['name'],status=status,
                               parameters=r['parameters'],snapshot=r['snapshot'],checksum=r['checksum'],
                               result=r['result'],error=r.get('error',''),created_at=r['created_at']))
                    if status=='queued': requeue.append(new_run_id)
                for v in manifest['reviews']:
                    db.add(Review(id=mapping['review'][v['id']],run_id=mapping['run'][v['run_id']],
                                  note=v['note'],author=v['author'],created_at=v['created_at']))
                os.makedirs(artifact_root(),exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    for item in manifest['artifacts']:
                        target=os.path.join(artifact_root(),f"run-{mapping['run'][item['run_id']]}.json")
                        with open(target,'wb') as fh: fh.write(zf.read(item['file']))
                log(db,user,'package.import',
                    f'{source_station}/study {source_study_id} → study {new_study_id} / {package_checksum[:12]}')
                db.commit()
        except Exception:
            db.rollback()
            with Session() as fail_db:
                b=fail_db.get(ImportBatch,batch_id)
                if b: b.status='failed';b.error='发布失败，已整体回滚';b.finished_at=now();fail_db.commit()
            raise
    with Session() as done_db:
        b=done_db.get(ImportBatch,batch_id)
        b.status='completed';b.conflict_note=conflict;b.finished_at=now();done_db.commit()
        for run_id in requeue: _enqueue(run_id)
        # Build the plain-data summary while the session is still open, so
        # callers never touch a detached ORM instance.
        return _batch_summary(b)

def _enqueue(run_id):
    try:
        import redis
        redis.Redis.from_url(os.environ['REDIS_URL']).rpush('analysis-queue',run_id)
    except Exception:
        # Worker startup recovery also re-enqueues queued runs; never block
        # an otherwise committed import on queue availability.
        logging.exception('enqueue after import failed; worker recovery will pick it up')

def _batch_summary(batch,idempotent=False):
    m=_normalize_mapping(batch.mapping or {})
    return {'id':batch.id,'source_station':batch.source_station,
            'source_study_id':batch.source_study_id,'package_checksum':batch.package_checksum,
            'status':batch.status,'idempotent':idempotent,
            'new_study_id':m.get('study',{}).get(batch.source_study_id),
            'counts':{k:len(v) for k,v in m.items() if isinstance(v,dict)},
            'conflict_note':batch.conflict_note or '','error':batch.error or '',
            'created_at':batch.created_at,'finished_at':batch.finished_at}

def list_batches(db):
    return [_batch_summary(b) for b in db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()))]

def preview(raw):
    # Numbering collision check before anything is written.
    manifest,package_checksum=_verify_package(raw)
    with Session() as db:
        name_taken=bool(db.scalar(select(Study).where(Study.name==manifest['study']['name'])))
        same=db.scalar(select(ImportBatch).where(
            ImportBatch.source_station==str(manifest['station_id']),
            ImportBatch.source_study_id==manifest['study']['id']))
    return {'source_station':manifest['station_id'],
            'source_study_id':manifest['study']['id'],
            'study_name':manifest['study']['name'],
            'package_checksum':package_checksum,
            'name_collision':name_taken,
            'same_source_state':same.status if same else None,
            'same_source_checksum':same.package_checksum if same else None,
            'content_changed':bool(same and same.package_checksum!=package_checksum),
            'counts':{'observations':len(manifest['observations']),
                      'runs':len(manifest['runs']),
                      'reviews':len(manifest['reviews']),
                      'artifacts':len(manifest['artifacts'])}}
