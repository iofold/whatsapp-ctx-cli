import pytest

from wactx import entities


def test_spacy_ner_pipeline_loads_and_extracts():
    # enrich crashed on Python 3.14 with spaCy < 3.8.18 (pydantic v1 in confection);
    # nothing exercised the spaCy path before this test.
    pytest.importorskip("en_core_web_sm")
    entities._NLP = None
    nlp = entities._get_nlp()
    assert nlp.has_pipe("ner"), "en_core_web_sm should load with its NER pipe"
    [found] = entities._extract_spacy(
        ["Satya Nadella met Sundar Pichai at Google in London."], ["Someone Else"]
    )
    assert found, "expected at least one entity from a sentence full of named entities"


def _naive_merges(rows):
    """The original dedup loop (difflib ratio on every pair), as the reference."""
    from difflib import SequenceMatcher

    by_type = {}
    for etype, val, count in rows:
        by_type.setdefault(etype, []).append((val, count))
    merges = {}
    for etype, values in by_type.items():
        values.sort(key=lambda x: -x[1])
        canonicals = []
        for val, count in values:
            v_lower, merged = val.lower(), False
            for canon, canon_count in canonicals:
                c_lower = canon.lower()
                if v_lower == c_lower:
                    merges[etype, val], merged = canon, True
                    break
                if len(v_lower) > 3 and len(c_lower) > 3 and (v_lower in c_lower or c_lower in v_lower):
                    shorter = val if len(val) < len(canon) else canon
                    longer = canon if shorter == val else val
                    target = longer if count < canon_count else shorter
                    if target != val:
                        merges[etype, val] = target
                    merged = True
                    break
                if len(v_lower) > 5 and len(c_lower) > 5 and SequenceMatcher(None, v_lower, c_lower).ratio() > 0.85:
                    merges[etype, val], merged = canon, True
                    break
            if not merged:
                canonicals.append((val, count))
    return merges


def test_compute_entity_merges_matches_the_naive_loop():
    import random

    rng = random.Random(3)
    base = ["Acme Robotics", "Northwind Traders", "Initech Systems", "Globex", "Umbrella Labs", "Hooli"]
    rows = []
    for i in range(1500):
        word = rng.choice(base)
        mutated = "".join(c for c in word if rng.random() > 0.08) + rng.choice(["", " Inc", "s", "", str(i % 7)])
        rows.append((rng.choice(["org", "person"]), mutated if rng.random() > 0.3 else mutated.upper(), rng.randint(2, 40)))
    rows = list({(t, v): (t, v, c) for t, v, c in rows}.values())
    assert entities.compute_entity_merges(list(rows)) == _naive_merges(list(rows))


def _entity_db():
    import duckdb

    conn = duckdb.connect()
    entities.ensure_entity_table(conn)
    rows = [("m1", "c1", "org", "Acme Corp"), ("m1", "c1", "org", "acme corp"),  # m1 already has both
            ("m2", "c1", "org", "Acme Corp"), ("m3", "c1", "org", "Acme Corp"), ("m4", "c2", "org", "acme corp")]
    conn.executemany("INSERT INTO extracted_entities VALUES (?, ?, ?, ?)", rows)
    return conn


def test_dedup_merges_even_when_the_message_already_has_the_canonical_value():
    conn = _entity_db()
    assert entities.dedup_entities(conn) == 1
    got = sorted(conn.execute("SELECT message_id, entity_value FROM extracted_entities").fetchall())
    assert got == [("m1", "Acme Corp"), ("m2", "Acme Corp"), ("m3", "Acme Corp"), ("m4", "Acme Corp")]
    assert entities.dedup_entities(conn) == 0  # idempotent


async def test_messages_without_entities_are_not_rescanned(monkeypatch):
    import duckdb

    from wactx.config import Config

    conn = duckdb.connect()
    conn.execute("CREATE TABLE messages (id VARCHAR, chat_jid VARCHAR, push_name VARCHAR, text_content VARCHAR, is_group BOOLEAN, timestamp TIMESTAMP)")
    conn.executemany(
        "INSERT INTO messages VALUES (?, ?, 'x', ?, true, now())",
        [("a", "g1", "ok thanks"), ("b", "g1", "see you"), ("a", "g2", "lunch?")],  # id 'a' repeats across chats
    )
    seen = []
    monkeypatch.setattr(entities, "_extract_spacy", lambda texts, senders: seen.append(len(texts)) or [[] for _ in texts])
    cfg = Config()
    assert await entities.extract_entities(conn, cfg) == 0
    assert await entities.extract_entities(conn, cfg) == 0
    conn.execute("INSERT INTO messages VALUES ('c', 'g1', 'x', 'new one', true, now())")
    await entities.extract_entities(conn, cfg)
    assert seen == [3, 1]  # all three once (both 'a's), then only the new message


def test_dedup_leaves_the_same_value_in_another_type_alone():
    # "Pragati" is a person and an org on the live DB. Merging the person spelling must not
    # rename or delete the org rows (merges used to be keyed by value only).
    import duckdb

    conn = duckdb.connect()
    entities.ensure_entity_table(conn)
    rows = [("m1", "c1", "person", "Pragati Rao"), ("m2", "c1", "person", "Pragati Rao"),
            ("m3", "c1", "person", "pragati rao"), ("m4", "c1", "person", "pragati rao"),
            ("m5", "c1", "org", "pragati rao"), ("m6", "c1", "org", "pragati rao")]
    conn.executemany("INSERT INTO extracted_entities VALUES (?, ?, ?, ?)", rows)
    assert entities.dedup_entities(conn) == 1
    got = conn.execute("SELECT message_id, entity_type, entity_value FROM extracted_entities ORDER BY 1").fetchall()
    assert got == [("m1", "person", "Pragati Rao"), ("m2", "person", "Pragati Rao"), ("m3", "person", "Pragati Rao"),
                   ("m4", "person", "Pragati Rao"), ("m5", "org", "pragati rao"), ("m6", "org", "pragati rao")]
