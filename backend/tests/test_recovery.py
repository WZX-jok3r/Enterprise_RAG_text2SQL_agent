import json

import pytest

from app.analytics.recovery import seal, verify, drill


def test_seal_verifies_binary_and_nested_manifest(tmp_path):
    (tmp_path / 'pg.dump').write_bytes(bytes(range(256)))
    (tmp_path / 'knowledge_base').mkdir()
    (tmp_path / 'knowledge_base' / 'manifest.json').write_text('{}')
    manifest = seal(tmp_path)
    assert len(manifest['files']) == 2
    verify(tmp_path)


@pytest.mark.parametrize('action', ['change', 'delete', 'extra'])
def test_corrupt_bundle_rejected(tmp_path, action):
    path = tmp_path / 'pg.dump'
    path.write_bytes(b'backup')
    seal(tmp_path)
    if action == 'change':
        path.write_bytes(b'corrupt')
    elif action == 'delete':
        path.unlink()
    else:
        (tmp_path / 'extra').write_bytes(b'extra')
    with pytest.raises(ValueError):
        verify(tmp_path)


def test_traversal_rejected(tmp_path):
    (tmp_path / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'files': {'../secret': 'x'}}))
    with pytest.raises(ValueError):
        verify(tmp_path)


def test_existing_output_fails_before_any_mutation(tmp_path):
    with pytest.raises(ValueError, match='new output'):
        drill(tmp_path, 'unused')


def test_prune_preserves_unrelated_and_failed_directories(tmp_path):
    from app.analytics.backup import prune
    for name in ['20260101_000000', '20260102_000000', '20260103_000000', 'important']:
        folder = tmp_path / name
        folder.mkdir()
        if name != '20260103_000000':
            (folder / 'manifest.json').write_text('{}')
    removed = prune(tmp_path, 1)
    assert [p.name for p in removed] == ['20260101_000000']
    assert (tmp_path / 'important').exists()
    assert (tmp_path / '20260103_000000').exists()
    with pytest.raises(ValueError):
        prune(tmp_path, 0)


def test_saved_restore_rejects_corruption_before_output_creation(tmp_path):
    from app.analytics.recovery import restore_saved
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    (bundle / 'pg_0.dump').write_bytes(b'backup')
    seal(bundle)
    (bundle / 'pg_0.dump').write_bytes(b'bad')
    output = tmp_path / 'restore'
    with pytest.raises(ValueError, match='checksum'):
        restore_saved(bundle, output, 'unused')
    assert not output.exists()


@pytest.mark.parametrize('wrong_index', [False, True])
def test_logical_restore_batches_and_checks_index(monkeypatch, wrong_index):
    from app.analytics import recovery
    config = {'config': {'params': {'vectors': {'size': 2, 'distance': 'Cosine'}}},
              'payload_schema': {'tenant_id': {'data_type': 'keyword'}}}
    calls = []
    class Response:
        def json(self):
            restored = dict(config)
            if wrong_index:
                restored['payload_schema'] = {}
            return {'result': restored}
    def request(session, base, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return Response()
    monkeypatch.setattr(recovery, 'qd', request)
    values = [{'id': index, 'vector': [1., 0.], 'payload': {}} for index in range(130)]
    if wrong_index:
        with pytest.raises(ValueError, match='index'):
            recovery.restore_logical(None, 'local', 'target', config, values)
    else:
        recovery.restore_logical(None, 'local', 'target', config, values)
    batches = [len(c[2]['json']['points']) for c in calls if '/points?' in c[1]]
    assert batches == [64, 64, 2]
