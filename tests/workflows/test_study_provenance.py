import hashlib
import json

import pytest

from workflows.quant.studies import provenance


OLD = "jobs/research_example.py"
NEW = "workflows/quant/studies/example.py"
ORIGINAL = b"# original research source\r\n"
MOVED = b"# relocated research source\n"
BEFORE = hashlib.sha256(ORIGINAL).hexdigest()
AFTER = hashlib.sha256(MOVED).hexdigest()


@pytest.fixture
def study(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "_relocations", lambda: {OLD: (NEW, BEFORE, AFTER)})
    live = tmp_path / NEW
    live.parent.mkdir(parents=True)
    live.write_bytes(MOVED)
    folder = tmp_path / "output" / "experiment"
    folder.mkdir(parents=True)
    return tmp_path, folder / "declaration.json"


def declared(hashes):
    return {"code_sha256": hashes, "parameters": {"version": 4}, "inputs": {"prices.csv": "frozen"}}


def test_new_manifest_and_archive_use_actual_canonical_bytes(study):
    root, manifest = study
    value = declared(provenance.source_hashes(root, [NEW]))
    provenance.freeze(manifest, value, source_root=root)
    assert json.loads(manifest.read_text(encoding="utf-8")) == declared({NEW: AFTER})
    assert (manifest.parent / "source" / NEW).read_bytes() == MOVED
    (root / NEW).write_bytes(b"changed research")
    with pytest.raises(ValueError, match="Frozen experiment changed"):
        provenance.freeze(manifest, declared(provenance.source_hashes(root, [NEW])), source_root=root)
    (root / NEW).unlink()
    with pytest.raises(FileNotFoundError):
        provenance.source_hashes(root, [NEW])


@pytest.mark.parametrize("previous_archive", [False, True])
def test_resume_preserves_legacy_manifest_and_archives(study, previous_archive):
    root, manifest = study
    frozen = json.dumps(declared({OLD: BEFORE}), sort_keys=True).encode()
    manifest.write_bytes(frozen)
    source = manifest.parent / "source"
    if previous_archive:
        (source / OLD).parent.mkdir(parents=True)
        (source / OLD).write_bytes(ORIGINAL)
    provenance.freeze(manifest, declared({NEW: AFTER}), source_root=root)
    assert manifest.read_bytes() == frozen
    if previous_archive:
        assert (source / OLD).read_bytes() == ORIGINAL
        assert not (source / NEW).exists()
        (source / OLD).write_bytes(b"damaged archive")
        with pytest.raises(ValueError, match="archive changed"):
            provenance.freeze(manifest, declared({NEW: AFTER}), source_root=root)
    else:
        assert (source / NEW).read_bytes() == MOVED
        assert not (source / OLD).exists()


def test_only_reviewed_hash_pair_and_full_manifest_may_match(study):
    old = declared({OLD: BEFORE})
    current = declared({NEW: AFTER})
    assert provenance.declaration_matches(old, current)
    for hashes in ({OLD: AFTER}, {OLD: "older released revision"}, {OLD: BEFORE, NEW: AFTER}):
        assert not provenance.declaration_matches(declared(hashes), current)
    for hashes in ({}, {NEW: BEFORE}, {NEW: "later changes"}, {NEW: AFTER, "extra.py": AFTER}):
        assert not provenance.declaration_matches(old, declared(hashes))
    assert not provenance.declaration_matches(old, {**current, "parameters": {"version": 5}})
    assert not provenance.declaration_matches(old, {**current, "inputs": {"prices.csv": "changed"}})


def test_declared_source_changed_before_archive(study):
    root, manifest = study
    (root / NEW).write_bytes(b"changed after hashing")
    with pytest.raises(ValueError, match="changed while archiving"):
        provenance.freeze(manifest, declared({NEW: AFTER}), source_root=root)
    assert not (manifest.parent / "source" / NEW).exists()
    (root / NEW).write_bytes(MOVED)
    provenance.freeze(manifest, declared({NEW: AFTER}), source_root=root)
    assert (manifest.parent / "source" / NEW).read_bytes() == MOVED
