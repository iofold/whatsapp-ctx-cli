import subprocess
from types import SimpleNamespace

import duckdb

import wactx.embed as embed
import wactx.sync as sync
from wactx.config import Config
from wactx.db import get_connection
from wactx.embed import HNSW_INDEX_NAME, create_hnsw_index, drop_hnsw_index, ensure_embedding_column

DIMS = 4


def _index_exists(conn) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM duckdb_indexes() WHERE index_name = ?", [HNSW_INDEX_NAME]
    ).fetchone()
    return bool(row and row[0])


def _seed(cfg: Config, embedded: int, pending: int) -> None:
    conn = get_connection(cfg)
    ensure_embedding_column(conn, DIMS)
    for i in range(embedded + pending):
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender_jid, text_content, embedding) VALUES (?, 'c', 's', ?, ?)",
            [f"m{i}", f"text {i}", [0.1, 0.2, 0.3, float(i)] if i < embedded else None],
        )
    create_hnsw_index(conn)
    assert _index_exists(conn)
    conn.close()


def _config(tmp_path) -> Config:
    cfg = Config(db_path=tmp_path / "test.duckdb")
    cfg.api.embedding_dims = DIMS
    return cfg


def test_drop_hnsw_index(tmp_path):
    cfg = _config(tmp_path)
    _seed(cfg, embedded=3, pending=0)
    conn = get_connection(cfg)
    assert drop_hnsw_index(conn) is True
    assert not _index_exists(conn)
    assert drop_hnsw_index(conn) is False


def test_sync_drops_index_while_binary_writes_then_rebuilds(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, embedded=3, pending=0)
    seen_during_run = []

    def fake_run(cmd, check):
        conn = duckdb.connect(str(cfg.db_path))
        seen_during_run.append(_index_exists(conn))
        conn.execute("INSERT INTO messages (id, chat_jid, sender_jid, text_content) VALUES ('new', 'c', 's', 'hi')")
        conn.close()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sync, "_require_binary", lambda _cfg: tmp_path / "whatsapp-sync")
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    sync.sync_whatsapp(cfg)

    assert seen_during_run == [False]
    conn = get_connection(cfg)
    assert _index_exists(conn)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 4


async def test_run_pipeline_embeds_without_index_then_rebuilds(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, embedded=2, pending=3)

    class FakeEmbeddings:
        async def create(self, model, input, dimensions):
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.5] * dimensions) for _ in input])

    monkeypatch.setattr(embed, "AsyncOpenAI", lambda **_kw: SimpleNamespace(embeddings=FakeEmbeddings()))

    real_drop = embed.drop_hnsw_index
    drops = []

    def spy_drop(conn):
        dropped = real_drop(conn)
        drops.append(dropped)
        return dropped

    monkeypatch.setattr(embed, "drop_hnsw_index", spy_drop)
    await embed.run_pipeline(cfg)

    assert drops == [True]
    conn = get_connection(cfg)
    assert _index_exists(conn)
    assert conn.execute("SELECT count(*) FROM messages WHERE embedding IS NULL").fetchone()[0] == 0


async def test_run_pipeline_keeps_index_when_nothing_to_embed(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _seed(cfg, embedded=3, pending=0)
    monkeypatch.setattr(embed, "drop_hnsw_index", lambda _conn: (_ for _ in ()).throw(AssertionError("dropped")))
    await embed.run_pipeline(cfg)
    conn = get_connection(cfg)
    assert _index_exists(conn)
