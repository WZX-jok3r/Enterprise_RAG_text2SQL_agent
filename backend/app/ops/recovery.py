"""Local, isolated recovery drill. Never restore over a source database/collection."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
import requests

from app.core.config import settings


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def seal(root: Path) -> dict:
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Symlinks are not supported in recovery bundles')
        if path.is_file() and path != root / 'manifest.json':
            files[path.relative_to(root).as_posix()] = digest(path)
    result = {'schema_version': 1, 'files': files}
    (root / 'manifest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def verify(root: Path) -> None:
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or not manifest.get('files'):
        raise ValueError('Invalid or empty recovery manifest')
    if any(p.is_symlink() for p in root.rglob('*')):
        raise ValueError('Unsafe recovery symlink')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*')
              if p.is_file() and p != root / 'manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('Recovery file inventory mismatch')
    for name, expected in manifest['files'].items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise ValueError('Unsafe recovery path')
        if digest(path) != expected:
            raise ValueError('Recovery checksum mismatch')


def connection(database: str):
    return psycopg.connect(host=settings.postgres_host, port=settings.postgres_port,
                          user=settings.postgres_user, password=settings.postgres_password,
                          dbname=database, autocommit=True)


def fingerprint(database: str) -> dict:
    result = {}
    with connection(database) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            tables = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
            for (table,) in tables:
                rows = conn.execute(sql.SQL('SELECT row_to_json(t)::text FROM public.{} t').format(sql.Identifier(table)))
                hashes = sorted(hashlib.sha256(row[0].encode()).hexdigest() for row in rows)
                result[table] = {'rows': len(hashes), 'sha256': hashlib.sha256(''.join(hashes).encode()).hexdigest()}
    return result


def pg(command: list[str], **kwargs):
    proc = subprocess.run(command, stderr=subprocess.PIPE, **kwargs)
    if proc.returncode:
        raise RuntimeError('PostgreSQL backup/restore command failed')


def qd(session, base, method, path, **kwargs):
    response = session.request(method, base + path, timeout=300, **kwargs)
    response.raise_for_status()
    return response


def points(session, base, collection, export=None):
    values = []
    offset = None
    while True:
        body = {'limit': 128, 'with_payload': True, 'with_vector': True}
        if offset is not None:
            body['offset'] = offset
        result = qd(session, base, 'POST', f'/collections/{collection}/points/scroll', json=body).json()['result']
        values.extend(result['points'])
        offset = result['next_page_offset']
        if offset is None:
            break
    canonical = json.dumps(sorted(values, key=lambda x: str(x['id'])), sort_keys=True, separators=(',', ':'))
    if export is not None:
        export.write_text(canonical, encoding='utf-8')
    return {'count': len(values), 'sha256': hashlib.sha256(canonical.encode()).hexdigest()}


def restore_logical(session, base, target, config, exported):
    params = config['config']['params']
    body = {key: params[key] for key in ['vectors', 'sparse_vectors', 'on_disk_payload'] if key in params}
    for key in ['hnsw_config', 'optimizers_config', 'quantization_config']:
        if config['config'].get(key) is not None:
            body[key] = config['config'][key]
    qd(session, base, 'PUT', f'/collections/{target}', json=body)
    for field, spec in config.get('payload_schema', {}).items():
        schema = spec.get('params') or spec['data_type']
        qd(session, base, 'PUT', f'/collections/{target}/index?wait=true',
           json={'field_name': field, 'field_schema': schema})
    for offset in range(0, len(exported), 64):
        qd(session, base, 'PUT', f'/collections/{target}/points?wait=true', json={'points': exported[offset:offset+64]})
    restored = qd(session, base, 'GET', f'/collections/{target}').json()['result']
    if restored['config']['params']['vectors'] != params['vectors']:
        raise ValueError('Restored vector configuration mismatch')
    if restored['config']['params'].get('sparse_vectors') != params.get('sparse_vectors'):
        raise ValueError('Restored sparse configuration mismatch')
    for field, spec in config.get('payload_schema', {}).items():
        actual = restored.get('payload_schema', {}).get(field, {})
        if actual.get('data_type') != spec.get('data_type') or actual.get('params') != spec.get('params'):
            raise ValueError('Restored payload index mismatch')


def drill(output: Path, container: str) -> dict:
    if output.exists():
        raise ValueError('Use a new output directory for each drill')
    output.mkdir(parents=True)
    bundle = output / 'bundle'
    bundle.mkdir()
    run_id = uuid.uuid4().hex[:16]
    prefix = 'recovery_' + run_id
    created_dbs, created_collections, snapshots = [], [], []
    report = {'schema_version': 1, 'run_id': run_id, 'passed': False,
              'scope': 'local live-source isolated restore; no external model calls', 'steps': []}
    start = time.perf_counter()
    session = requests.Session()
    session.trust_env = False  # Explicit local service; ignore external proxy configuration.
    base = f'http://{settings.qdrant_host}:{settings.qdrant_port}'
    try:
        before = {db: fingerprint(db) for db in [settings.postgres_db, settings.analytics_db]}
        names = [c['name'] for c in qd(session, base, 'GET', '/collections').json()['result']['collections']]
        vectors = {name: points(session, base, name, bundle / f'points_{index}.json') for index, name in enumerate(names)}
        configs = {name: qd(session, base, 'GET', f'/collections/{name}').json()['result'] for name in names}
        (bundle / 'qdrant_config.json').write_text(json.dumps(configs), encoding='utf-8')
        for index, db in enumerate(before):
            with (bundle / f'pg_{index}.dump').open('wb') as stream:
                pg(['docker', 'exec', container, 'pg_dump', '-U', settings.postgres_user, '-d', db, '-Fc'], stdout=stream)
        for index, name in enumerate(names):
            snapshot = qd(session, base, 'POST', f'/collections/{name}/snapshots').json()['result']['name']
            snapshots.append((name, snapshot))
            response = qd(session, base, 'GET', f'/collections/{name}/snapshots/{snapshot}', stream=True)
            with response, (bundle / f'qd_{index}.snapshot').open('wb') as stream:
                for chunk in response.iter_content(1024 * 1024):
                    stream.write(chunk)
        for name, source in [('knowledge_base', settings.knowledge_base_dir),
                             ('processed_cache', settings.processed_cache_file.parent)]:
            if source.exists():
                if any(p.is_symlink() for p in source.rglob('*')):
                    raise ValueError('Source contains symlinks')
                shutil.copytree(source, bundle / name)
        metadata = {'databases': list(before), 'collections': names, 'tables': before, 'points': vectors}
        (bundle / 'inventory.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        seal(bundle)
        verify(bundle)
        report['steps'].append({'name': 'backup_checksum', 'passed': True})
        backup_end = time.perf_counter()
        # Restoration is gated by a successful checksum verification.
        for index, db in enumerate(before):
            target = prefix + f'_{index}'
            with connection(settings.postgres_db) as conn:
                conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target)))
            created_dbs.append(target)
            with (bundle / f'pg_{index}.dump').open('rb') as stream:
                pg(['docker', 'exec', '-i', container, 'pg_restore', '-U', settings.postgres_user,
                    '-d', target, '--exit-on-error', '--no-owner'], stdin=stream, stdout=subprocess.DEVNULL)
            if fingerprint(target) != before[db]:
                raise ValueError('Restored database differs from pre-backup source; source may have changed')
        report['steps'].append({'name': 'postgres_restore_content', 'passed': True})
        for index, name in enumerate(names):
            target = prefix + f'_qd_{index}'
            created_collections.append(target)
            try:
                with (bundle / f'qd_{index}.snapshot').open('rb') as stream:
                    qd(session, base, 'POST', f'/collections/{target}/snapshots/upload?priority=snapshot', files={'snapshot': stream})
            except requests.HTTPError as exc:
                # Preserve the failed physical-backup result; independently verify a logical backup.
                report.setdefault('snapshot_failures', []).append({'collection': name, 'http_status': exc.response.status_code})
                qd(session, base, 'DELETE', f'/collections/{target}')
                exported = json.loads((bundle / f'points_{index}.json').read_text())
                restore_logical(session, base, target, configs[name], exported)
            if points(session, base, target) != vectors[name]:
                raise ValueError('Restored vectors/payload differ from source')
        report['steps'].append({'name': 'qdrant_restore_vectors_payload', 'passed': True})
        restored = output / 'restored_files'
        restored.mkdir()
        for name in ['knowledge_base', 'processed_cache']:
            if (bundle / name).exists():
                shutil.copytree(bundle / name, restored / name)
        for name, expected in json.loads((bundle / 'manifest.json').read_text())['files'].items():
            if name.startswith(('knowledge_base/', 'processed_cache/')) and digest(restored / name) != expected:
                raise ValueError('Restored file mismatch')
        # Drift after backup must never be reported as a successful consistency drill.
        if {db: fingerprint(db) for db in before} != before:
            raise ValueError('Source database changed during drill; repeat in a write-free window')
        if any(points(session, base, name) != vectors[name] for name in names):
            raise ValueError('Source collection changed during drill')
        for name in ['knowledge_base', 'processed_cache']:
            source = settings.base_dir / name
            if source.exists():
                expected_files = {p.relative_to(bundle / name).as_posix() for p in (bundle / name).rglob('*') if p.is_file()}
                if {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()} != expected_files:
                    raise ValueError('Source file inventory changed during drill')
                for path in source.rglob('*'):
                    if path.is_file() and (not (bundle / name / path.relative_to(source)).exists()
                                           or digest(path) != digest(bundle / name / path.relative_to(source))):
                        raise ValueError('Source files changed during drill')
        report['steps'].append({'name': 'files_restore_and_source_stability', 'passed': True})
        report.update(passed=True, backup_seconds=round(backup_end - start, 3),
                      restore_verify_seconds=round(time.perf_counter() - backup_end, 3),
                      databases=before, collections=vectors,
                      bundle_files=len(json.loads((bundle / 'manifest.json').read_text())['files']),
                      rpo='not measured: no outage or scheduled backup cadence',
                      rto='component restore and verification timing only; not full service recovery')
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        raise
    finally:
        cleanup_errors = []
        for target in created_collections:
            try:
                qd(session, base, 'DELETE', f'/collections/{target}')
            except Exception:
                cleanup_errors.append(target)
        for target in created_dbs:
            try:
                with connection(settings.postgres_db) as conn:
                    conn.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))
            except Exception:
                cleanup_errors.append(target)
        for name, snapshot in snapshots:
            try:
                qd(session, base, 'DELETE', f'/collections/{name}/snapshots/{snapshot}')
            except Exception:
                cleanup_errors.append('snapshot:' + name)
        session.close()
        report['cleanup_errors'] = cleanup_errors
        if cleanup_errors:
            report['passed'] = False
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def restore_saved(bundle: Path, output: Path, container: str) -> dict:
    """Prove stored logical data can recover without reading source collections/files."""
    verify(bundle)
    metadata = json.loads((bundle / 'inventory.json').read_text(encoding='utf-8'))
    configs = json.loads((bundle / 'qdrant_config.json').read_text(encoding='utf-8'))
    if output.exists():
        raise ValueError('Use a new output directory')
    output.mkdir(parents=True)
    prefix = 'recovery_' + uuid.uuid4().hex[:16]
    databases, collections = [], []
    session = requests.Session()
    session.trust_env = False
    base = f'http://{settings.qdrant_host}:{settings.qdrant_port}'
    report = {'passed': False, 'steps': [], 'scope': 'saved bundle logical recovery, source data not read'}
    started = time.perf_counter()
    try:
        for index, name in enumerate(metadata['databases']):
            target = prefix + f'_{index}'
            with connection('postgres') as conn:
                conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target)))
            databases.append(target)
            with (bundle / f'pg_{index}.dump').open('rb') as stream:
                pg(['docker', 'exec', '-i', container, 'pg_restore', '-U', settings.postgres_user,
                    '-d', target, '--exit-on-error', '--no-owner'], stdin=stream, stdout=subprocess.DEVNULL)
            if fingerprint(target) != metadata['tables'][name]:
                raise ValueError('Restored database content mismatch')
        report['steps'].append({'name': 'postgres', 'passed': True})
        for index, name in enumerate(metadata['collections']):
            target = prefix + f'_qd_{index}'
            collections.append(target)
            exported = json.loads((bundle / f'points_{index}.json').read_text(encoding='utf-8'))
            restore_logical(session, base, target, configs[name], exported)
            if points(session, base, target) != metadata['points'][name]:
                raise ValueError('Restored vectors mismatch')
        report['steps'].append({'name': 'logical_vectors_payload_indexes', 'passed': True})
        for name in ['knowledge_base', 'processed_cache']:
            if (bundle / name).exists():
                shutil.copytree(bundle / name, output / name)
        manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
        for name, expected in manifest['files'].items():
            if name.startswith(('knowledge_base/', 'processed_cache/')) and digest(output / name) != expected:
                raise ValueError('Restored file checksum mismatch')
        report['steps'].append({'name': 'files', 'passed': True})
        report.update(passed=True, restore_verify_seconds=round(time.perf_counter()-started, 3),
                      point_counts={name: item['count'] for name, item in metadata['points'].items()},
                      table_counts={name: len(tables) for name, tables in metadata['tables'].items()})
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        raise
    finally:
        errors = []
        for target in collections:
            try:
                qd(session, base, 'DELETE', f'/collections/{target}')
            except Exception:
                errors.append(target)
        for target in databases:
            try:
                with connection('postgres') as conn:
                    conn.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))
            except Exception:
                errors.append(target)
        session.close()
        report['cleanup_errors'] = errors
        if errors:
            report['passed'] = False
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--postgres-container', default='docker_setting-postgres-1')
    parser.add_argument('--bundle', type=Path, help='Restore an existing drill bundle using verified logical vectors')
    args = parser.parse_args()
    try:
        if args.bundle:
            result = restore_saved(args.bundle.resolve(), args.output.resolve(), args.postgres_container)
        else:
            result = drill(args.output.resolve(), args.postgres_container)
        print(json.dumps({'passed': result['passed'], 'report': str(args.output / 'report.json')}))
        return 0 if result['passed'] else 1
    except Exception as exc:
        print('Recovery drill failed: ' + type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
