"""Deployment smoke checks against real containers and HTTP, without model calls."""
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import requests
from psycopg import sql
from app.analytics.recovery import connection
from app.core.config import settings
from app.repositories.document_version_repo import document_key


def command(args, **kwargs):
    process = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if process.returncode:
        raise RuntimeError('Container verification command failed: ' + process.stderr[-800:])
    return process.stdout


def manifest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ['app', 'migrations', 'eval']
            for p in (root / folder).rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py', '.json', '.jsonl'}}


PROBE = '''
import hashlib,json,pathlib
from app.core.config import settings
from app.analytics.executor import SqlExecutor
from app.database import db
from app.vector.version_visibility import current_revision_ids
from app.worker.settings import WorkerSettings
from app.core.performance import capture_measurements,measured_post
root=pathlib.Path('/app')
files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
 for folder in ['app','migrations','eval'] for p in (root/folder).rglob('*')
 if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.json','.jsonl'}}
settings.sql_audit_enabled=False
executor=SqlExecutor()
allowed=executor.execute('SELECT COUNT(*) FROM employees',redact=settings.redact_column_set)
assert allowed.ok
for query in ['SELECT salary FROM employees LIMIT 1','SELECT row_to_json(e) FROM employees e LIMIT 1']:
 assert not executor.execute(query,redact=settings.redact_column_set).ok
ids=current_revision_ids()
from app.analytics.recovery import connection
from app.repositories.document_version_repo import document_key
from app.vector.qdrant import VectorStore
from qdrant_client import QdrantClient,models
import uuid
token=uuid.uuid4().hex
tenant='runtime_probe_'+token[:20]
family=document_key(tenant,'runtime.txt')
collection='runtime_probe_'+token
client=QdrantClient(host=settings.qdrant_host,port=settings.qdrant_port)
doc_ids=[]
try:
 with connection(settings.postgres_db) as conn:
  for version in [1,2]:
   row=conn.execute("INSERT INTO documents(source,file_hash,tenant_id,format,status,document_key,version,original_filename,is_current) VALUES(%s,'synthetic',%s,'txt','ready',%s,%s,'runtime.txt',%s) RETURNING id",(token+'_'+str(version),tenant,family,version,version==2)).fetchone()
   doc_ids.append(row[0])
 client.create_collection(collection,vectors_config={'dense':models.VectorParams(size=2,distance=models.Distance.COSINE)})
 client.upsert(collection,points=[models.PointStruct(id=str(uuid.uuid4()),vector={'dense':[1.,0.]},payload={'text':'synthetic','document_version_id':str(doc_id),'tenant_id':[tenant]}) for doc_id in doc_ids],wait=True)
 class FixedEmbedding:
  def embed_single(self,query): return [1.,0.]
 store=object.__new__(VectorStore)
 store.client=client
 store.collection_name=collection
 store.embedding_client=FixedEmbedding()
 store.rerank_client=None
 settings.retrieval_mode='vector'
 assert [r['metadata']['document_version_id'] for r in store.search('synthetic',filter_dict={'tenant_id':tenant})]==[str(doc_ids[1])]
 assert store.search('synthetic',filter_dict={'tenant_id':'foreign-runtime-probe'})==[]
 with connection(settings.postgres_db) as conn:
  with conn.transaction():
   conn.execute('UPDATE documents SET is_current=false WHERE document_key=%s',(family,))
   conn.execute('UPDATE documents SET is_current=true WHERE id=%s',(doc_ids[0],))
 assert [r['metadata']['document_version_id'] for r in store.search('synthetic',filter_dict={'tenant_id':tenant})]==[str(doc_ids[0])]
finally:
 if client.collection_exists(collection): client.delete_collection(collection)
 client.close()
 with connection(settings.postgres_db) as conn:
  conn.execute('DELETE FROM documents WHERE document_key=%s',(family,))
import requests
original=requests.post
class Response:
 def raise_for_status(self): pass
 def json(self): return {'usage':{'prompt_tokens':3,'completion_tokens':2}}
requests.post=lambda *a,**k:Response()
try:
 with capture_measurements() as collector:
  measured_post('llm','synthetic-runtime','http://synthetic.invalid')
  assert len(collector.calls)==1
finally: requests.post=original
print(json.dumps({'manifest':files,'sql_allowed':True,'sensitive_denied':True,
 'performance_capture':True,'version_registry':True,'real_qdrant_version_visibility':True,'worker_registered':
 [f.__name__ for f in WorkerSettings.functions]}))
'''


def main():
    run = uuid.uuid4().hex
    report = {'passed': False, 'run_id': run, 'checks': [], 'external_model_calls': 0}
    output = ROOT / 'output' / 'runtime_acceptance.json'
    session = requests.Session()
    session.trust_env = False
    owned_ids = []
    task_id = None
    filename = 'runtime_' + run + '.txt'
    cleanup_errors = []
    try:
        images = json.loads(command(['docker', 'inspect', 'docker_setting-backend-1', 'docker_setting-worker-1']))
        assert images[0]['Image'] == images[1]['Image']
        assert all(item['State']['Running'] for item in images)
        report['image_id'] = images[0]['Image']
        expected = manifest(ROOT / 'backend')
        for name in ['docker_setting-backend-1', 'docker_setting-worker-1']:
            data = json.loads(command(['docker', 'exec', '-i', name, 'python', '-'], input=PROBE).splitlines()[-1])
            assert data.pop('manifest') == expected, name + ' source mismatch'
            assert data['worker_registered'] == ['ingest_document']
            report['checks'].append({'name': name + '_source_sql_registry_capture', 'passed': True,
                                     'details': data})
        report['source_files'] = len(expected)
        base = 'http://127.0.0.1:8000/api/v1'
        assert session.get(base + '/health', timeout=10).status_code == 200
        assert session.get('http://127.0.0.1:8080/api/v1/health', timeout=10).status_code == 200
        assert session.get('http://127.0.0.1:8080/', timeout=10).status_code == 200
        assert session.get(base + '/documents', timeout=10).status_code == 401
        assert session.get(base + '/documents', headers={'X-API-Key': 'invalid-runtime-key'}, timeout=10).status_code == 403
        keys = list(settings.tenant_keys)
        assert len(set(settings.tenant_keys.values())) >= 2, 'Need two configured tenants for ACL smoke'
        key = keys[0]
        tenant = settings.tenant_keys[key]
        foreign_key = next(k for k in keys if settings.tenant_keys[k] != tenant)
        headers, foreign = {'X-API-Key': key}, {'X-API-Key': foreign_key}
        assert session.get(base + '/documents', headers=headers, timeout=10).status_code == 200
        report['checks'].append({'name': 'health_frontend_proxy_and_auth', 'passed': True})
        # Whitespace TXT parses to zero documents before embedding; actual running worker handles it.
        response = session.post(base + '/documents', headers=headers,
                                files={'file': (filename, b' \n\t', 'text/plain')}, timeout=10)
        assert response.status_code == 202
        task_id = response.json()['task_id']
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status = session.get(base + f'/documents/{task_id}/status', headers=headers, timeout=10).json()
            if status['status'] in {'failed', 'done'}:
                break
            time.sleep(.5)
        assert status['status'] == 'failed', 'Blank revision must fail rather than publish'
        with connection(settings.postgres_db) as conn:
            row = conn.execute('SELECT d.id,d.document_key,d.version,d.is_current,d.status FROM documents d JOIN ingest_tasks t ON t.document_id=d.id WHERE t.id=%s', (task_id,)).fetchone()
            assert row and row[1] and row[2] == 1 and not row[3] and row[4] == 'failed'
            owned_ids.append(row[0])
        doc_id = row[0]
        for path in [f'/documents/{task_id}/status', f'/documents/{doc_id}/versions']:
            assert session.get(base + path, headers=foreign, timeout=10).status_code == 404
        assert session.delete(base + f'/documents/{doc_id}', headers=foreign, timeout=10).status_code == 404
        assert session.post(base + f'/documents/{doc_id}/activate', headers=headers, timeout=10).status_code == 409
        assert session.post(base + f'/documents/{doc_id}/activate', headers=foreign, timeout=10).status_code == 404
        report['checks'].append({'name': 'real_api_queue_worker_failed_revision_and_acl', 'passed': True})
        assert session.delete(base + f'/documents/{doc_id}', headers=headers, timeout=10).status_code == 200
        # READY fixtures verify actual HTTP rollback without invoking model/embedding.
        family = document_key(tenant, 'runtime_ready_' + run + '.txt')
        for version in [1, 2]:
            with connection(settings.postgres_db) as conn:
                row = conn.execute('INSERT INTO documents(source,file_hash,tenant_id,format,status,document_key,version,original_filename,is_current) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,false) RETURNING id',
                    ('runtime_ready_' + run + f'_{version}.txt', 'synthetic', tenant, 'txt', 'ready', family, version, 'runtime_ready_' + run + '.txt')).fetchone()
                owned_ids.append(row[0])
        first, second = owned_ids[-2:]
        for doc_id in [second, first]:
            result = session.post(base + f'/documents/{doc_id}/activate', headers=headers, timeout=10)
            assert result.status_code == 200 and result.json()['is_current']
            with connection(settings.postgres_db) as conn:
                rows = conn.execute('SELECT id FROM documents WHERE document_key=%s AND is_current', (family,)).fetchall()
                assert rows == [(doc_id,)]
        history = session.get(base + f'/documents/{first}/versions', headers=headers, timeout=10).json()
        assert [d['version'] for d in history['documents']] == [2, 1]
        report['checks'].append({'name': 'http_activate_rollback_history_ready_fixtures', 'passed': True})
        report['passed'] = True
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        raise
    finally:
        # Cleanup only resources with this run's UUID; never bulk-clear service data.
        try:
            with connection(settings.postgres_db) as conn:
                ids = conn.execute('SELECT id,source FROM documents WHERE original_filename IN (%s,%s)',
                                   (filename, 'runtime_ready_' + run + '.txt')).fetchall()
                for doc_id, source in ids:
                    conn.execute('DELETE FROM ingest_tasks WHERE document_id=%s', (doc_id,))
                    conn.execute('DELETE FROM documents WHERE id=%s', (doc_id,))
                    path = settings.knowledge_base_dir / '.versions' / source
                    if path.resolve().is_relative_to(settings.knowledge_base_dir.resolve()):
                        path.unlink(missing_ok=True)
            if task_id:
                from redis import Redis
                redis = Redis.from_url(settings.redis_url)
                redis.delete('arq:result:ingest:' + task_id, 'arq:job:ingest:' + task_id)
                redis.close()
        except Exception as exc:
            cleanup_errors.append(type(exc).__name__)
        session.close()
        report['cleanup_errors'] = cleanup_errors
        if cleanup_errors:
            report['passed'] = False
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
