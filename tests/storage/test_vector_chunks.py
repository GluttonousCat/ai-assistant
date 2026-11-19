from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from storage.repositories import vector_chunks as repo
from storage.schema import vector_chunks as schema


def test_collection_schemas_keep_field_order_limits_and_dimensions(monkeypatch):
    class Schema:
        def __init__(self):
            self.fields = []

        def add_field(self, name, kind, **options):
            self.fields.append((name, kind, options))

    monkeypatch.setattr(schema, "new_schema", Schema)
    report = schema.report_schema(128).fields
    annual = schema.annual_schema(256).fields
    assert [r[0] for r in report] == [
        "chunk_id",
        "report_id",
        "chunk_idx",
        "ts_code",
        "market",
        "org_name",
        "publish_year",
        "section",
        "chunk_type",
        "chunk_text",
        "embedding",
    ]
    assert [r[0] for r in annual] == [
        "chunk_id",
        "ts_code",
        "report_year",
        "category",
        "section_key",
        "section_title",
        "subsection_title",
        "chunk_text",
        "embedding",
    ]
    assert report[0][2] == {"is_primary": True, "max_length": 128}
    assert report[-2][2]["max_length"] == annual[-2][2]["max_length"] == 16384
    assert report[-1][2]["dim"] == 128 and annual[-1][2]["dim"] == 256


def test_first_use_is_coalesced_and_collections_keep_separate_caches(monkeypatch):
    client = object()
    monkeypatch.setattr(repo, "get_milvus", lambda: client)
    monkeypatch.setattr(repo, "milvus_config", lambda: {"embed_dim": 128})
    monkeypatch.setattr(repo, "collection_vector_dim", lambda *a: 128)
    monkeypatch.setattr(repo, "vector_index_params", lambda *a: {})
    create, rebuild = MagicMock(return_value=True), MagicMock()
    monkeypatch.setattr(repo, "ensure_collection", create)
    monkeypatch.setattr(repo, "rebuild_collection", rebuild)
    first = repo.ChunkCollection("first", lambda n: {"dim": n})
    second = repo.ChunkCollection("second", lambda n: {"dim": n})
    with ThreadPoolExecutor(12) as pool:
        assert all(pool.map(lambda _: first.ensure(), range(40)))
    assert create.call_count == 1
    second.ensure()
    assert create.call_count == 2
    first.rebuild()
    rebuild.assert_called_once_with(client, "first", {"dim": 128}, {})
    first.ensure()
    second.ensure()
    assert create.call_count == 3


def test_cleanup_remains_best_effort_and_uses_the_requested_collection(monkeypatch):
    delete = MagicMock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(repo, "delete_by_expr", delete)
    collection = repo.ChunkCollection("derived", lambda n: n)
    assert collection.delete("report_id == 7") == 0
    delete.assert_called_once_with("derived", "report_id == 7")


def test_old_collection_exports_share_the_canonical_operations():
    from skills.report import collection as report
    from skills.content import collection as annual
    from tools.finance import vector_collections as tools

    assert report.ensure_report_chunks is tools.ensure_report_chunks
    assert annual.delete_annual_chunks is tools.delete_annual_chunks
    assert callable(report._ensure_created.cache_clear)
