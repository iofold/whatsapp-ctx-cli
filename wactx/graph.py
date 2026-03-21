from __future__ import annotations

import logging
import time

import duckdb

from wactx.config import Config
from wactx.db import table_exists

log = logging.getLogger("wactx.graph")

SQL_VERTEX_PERSONS_NO_EMAIL = """
CREATE OR REPLACE TABLE graph_persons AS
WITH wa_persons AS (
    SELECT jid AS source_id, 'whatsapp' AS source,
           COALESCE(push_name, full_name, business_name, jid) AS display_name,
           push_name, full_name, NULL AS email_address
    FROM contacts WHERE is_group = false
)
SELECT ROW_NUMBER() OVER () AS person_id, source_id, source, display_name,
       push_name, full_name, email_address
FROM wa_persons;
"""

SQL_VERTEX_GROUPS = """
CREATE OR REPLACE TABLE graph_groups AS
SELECT jid AS group_jid,
       COALESCE(group_name, push_name, jid) AS group_name,
       (SELECT COUNT(DISTINCT sender_jid) FROM messages m
        WHERE m.chat_jid = c.jid AND m.is_group) AS member_count
FROM contacts c WHERE is_group = true;
"""

SQL_VERTEX_TOPICS = """
CREATE OR REPLACE TABLE graph_topics AS
SELECT ROW_NUMBER() OVER () AS topic_id,
       entity_value AS topic_name,
       entity_type AS source,
       COUNT(*) AS message_count
FROM extracted_entities
WHERE entity_type IN ('tech', 'org', 'event')
GROUP BY entity_value, entity_type
HAVING COUNT(*) >= 3;
"""

SQL_VERTEX_TOPICS_EMPTY = """
CREATE OR REPLACE TABLE graph_topics AS
SELECT
    CAST(NULL AS BIGINT) AS topic_id,
    CAST(NULL AS VARCHAR) AS topic_name,
    CAST(NULL AS VARCHAR) AS source,
    CAST(NULL AS BIGINT) AS message_count
WHERE FALSE;
"""

SQL_VERTEX_ENTITIES = """
CREATE OR REPLACE TABLE graph_entities AS
SELECT ROW_NUMBER() OVER () AS entity_id,
       entity_type, entity_value,
       COUNT(*) AS mention_count
FROM extracted_entities
GROUP BY entity_type, entity_value
HAVING COUNT(*) >= 1;
"""

SQL_EDGE_PERSON_MESSAGED = """
CREATE OR REPLACE TABLE edge_person_messaged AS
SELECT ROW_NUMBER() OVER () AS edge_id,
       gp_sender.person_id AS sender_person_id,
       gp_receiver.person_id AS receiver_person_id,
       COUNT(*) AS message_count,
       MIN(m.timestamp) AS first_message, MAX(m.timestamp) AS last_message
FROM messages m
JOIN graph_persons gp_sender ON m.sender_jid = gp_sender.source_id
JOIN graph_persons gp_receiver ON m.chat_jid = gp_receiver.source_id
WHERE m.is_group = false
GROUP BY gp_sender.person_id, gp_receiver.person_id;
"""

SQL_EDGE_PERSON_IN_GROUP = """
CREATE OR REPLACE TABLE edge_person_in_group AS
SELECT ROW_NUMBER() OVER () AS edge_id, gp.person_id, gg.group_jid,
       COUNT(*) AS message_count,
       MIN(m.timestamp) AS first_seen, MAX(m.timestamp) AS last_seen
FROM messages m
JOIN graph_persons gp ON m.sender_jid = gp.source_id
JOIN graph_groups gg ON m.chat_jid = gg.group_jid
WHERE m.is_group = true
GROUP BY gp.person_id, gg.group_jid;
"""

SQL_EDGE_PERSON_TOPIC = """
CREATE OR REPLACE TABLE edge_person_topic AS
SELECT ROW_NUMBER() OVER () AS edge_id, gp.person_id, gt.topic_id,
       COUNT(*) AS mention_count
FROM extracted_entities ee
JOIN messages m ON ee.message_id = m.id
JOIN graph_persons gp ON m.sender_jid = gp.source_id
JOIN graph_topics gt ON ee.entity_value = gt.topic_name AND ee.entity_type = gt.source
GROUP BY gp.person_id, gt.topic_id;
"""

SQL_EDGE_PERSON_TOPIC_EMPTY = """
CREATE OR REPLACE TABLE edge_person_topic AS
SELECT
    CAST(NULL AS BIGINT) AS edge_id,
    CAST(NULL AS BIGINT) AS person_id,
    CAST(NULL AS BIGINT) AS topic_id,
    CAST(NULL AS BIGINT) AS mention_count
WHERE FALSE;
"""

SQL_EDGE_PERSON_COOCCURS = """
CREATE OR REPLACE TABLE edge_person_cooccurs AS
WITH group_members AS (
    SELECT DISTINCT gp.person_id, m.chat_jid AS group_jid
    FROM messages m
    JOIN graph_persons gp ON m.sender_jid = gp.source_id
    WHERE m.is_group = true
)
SELECT ROW_NUMBER() OVER () AS edge_id,
       a.person_id AS person1_id, b.person_id AS person2_id,
       COUNT(DISTINCT a.group_jid) AS shared_groups
FROM group_members a
JOIN group_members b ON a.group_jid = b.group_jid AND a.person_id < b.person_id
GROUP BY a.person_id, b.person_id;
"""

SQL_EDGE_PERSON_CONVERSED = """
CREATE OR REPLACE TABLE edge_person_conversed AS
SELECT ROW_NUMBER() OVER () AS edge_id,
       gp1.person_id AS person1_id,
       gp2.person_id AS person2_id,
       m1.chat_jid AS group_jid,
       COUNT(*) AS exchange_count,
       MIN(m1.timestamp) AS first_exchange,
       MAX(m1.timestamp) AS last_exchange
FROM messages m1
JOIN messages m2 ON m1.chat_jid = m2.chat_jid
  AND m1.sender_jid != m2.sender_jid
  AND m2.timestamp BETWEEN m1.timestamp AND m1.timestamp + INTERVAL '5 minutes'
  AND m1.is_group = true
  AND m2.is_group = true
JOIN graph_persons gp1 ON m1.sender_jid = gp1.source_id
JOIN graph_persons gp2 ON m2.sender_jid = gp2.source_id
WHERE gp1.person_id < gp2.person_id
GROUP BY gp1.person_id, gp2.person_id, m1.chat_jid;
"""

SQL_EDGE_PERSON_MENTIONS_ENTITY = """
CREATE OR REPLACE TABLE edge_person_mentions_entity AS
SELECT ROW_NUMBER() OVER () AS edge_id,
       gp.person_id, ge.entity_id,
       COUNT(*) AS mention_count
FROM extracted_entities ee
JOIN messages m ON ee.message_id = m.id
JOIN graph_persons gp ON m.sender_jid = gp.source_id
JOIN graph_entities ge ON ee.entity_type = ge.entity_type AND ee.entity_value = ge.entity_value
GROUP BY gp.person_id, ge.entity_id;
"""


def _run_sql(conn: duckdb.DuckDBPyConnection, label: str, sql: str) -> None:
    start = time.time()
    conn.execute(sql)
    log.info("%-32s OK (%.2fs)", label, time.time() - start)


def _count_if_exists(conn: duckdb.DuckDBPyConnection, table_name: str) -> int:
    if not table_exists(conn, table_name):
        return 0
    row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    if row is None:
        return 0
    return int(row[0])


def build_vertex_tables(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    _run_sql(conn, "Create graph_persons", SQL_VERTEX_PERSONS_NO_EMAIL)
    _run_sql(conn, "Create graph_groups", SQL_VERTEX_GROUPS)

    if table_exists(conn, "extracted_entities"):
        _run_sql(conn, "Create graph_topics", SQL_VERTEX_TOPICS)
    else:
        _run_sql(conn, "Create empty graph_topics", SQL_VERTEX_TOPICS_EMPTY)

    if table_exists(conn, "extracted_entities"):
        _run_sql(conn, "Create graph_entities", SQL_VERTEX_ENTITIES)

    return {
        "graph_persons": _count_if_exists(conn, "graph_persons"),
        "graph_groups": _count_if_exists(conn, "graph_groups"),
        "graph_topics": _count_if_exists(conn, "graph_topics"),
        "graph_entities": _count_if_exists(conn, "graph_entities"),
    }


def build_edge_tables(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    _run_sql(conn, "Create edge_person_messaged", SQL_EDGE_PERSON_MESSAGED)
    _run_sql(conn, "Create edge_person_in_group", SQL_EDGE_PERSON_IN_GROUP)

    if (
        table_exists(conn, "extracted_entities")
        and _count_if_exists(conn, "graph_topics") > 0
    ):
        _run_sql(conn, "Create edge_person_topic", SQL_EDGE_PERSON_TOPIC)
    else:
        _run_sql(conn, "Create empty edge_person_topic", SQL_EDGE_PERSON_TOPIC_EMPTY)

    _run_sql(conn, "Create edge_person_cooccurs", SQL_EDGE_PERSON_COOCCURS)
    _run_sql(conn, "Create edge_person_conversed", SQL_EDGE_PERSON_CONVERSED)

    if table_exists(conn, "extracted_entities") and table_exists(
        conn, "graph_entities"
    ):
        _run_sql(
            conn,
            "Create edge_person_mentions_entity",
            SQL_EDGE_PERSON_MENTIONS_ENTITY,
        )

    return {
        "edge_person_messaged": _count_if_exists(conn, "edge_person_messaged"),
        "edge_person_in_group": _count_if_exists(conn, "edge_person_in_group"),
        "edge_person_topic": _count_if_exists(conn, "edge_person_topic"),
        "edge_person_cooccurs": _count_if_exists(conn, "edge_person_cooccurs"),
        "edge_person_conversed": _count_if_exists(conn, "edge_person_conversed"),
        "edge_person_mentions_entity": _count_if_exists(
            conn, "edge_person_mentions_entity"
        ),
    }


def create_property_graph(conn: duckdb.DuckDBPyConnection) -> None:
    has_entities = table_exists(conn, "graph_entities") and table_exists(
        conn, "edge_person_mentions_entity"
    )
    has_topics = (
        _count_if_exists(conn, "graph_topics") > 0
        and _count_if_exists(conn, "edge_person_topic") > 0
    )

    entity_vertex = ""
    entity_edge = ""
    if (
        has_entities
        and _count_if_exists(conn, "graph_entities") > 0
        and _count_if_exists(conn, "edge_person_mentions_entity") > 0
    ):
        entity_vertex = ",\n            graph_entities LABEL Entity"
        entity_edge = (
            ",\n            edge_person_mentions_entity"
            "\n                SOURCE KEY (person_id) REFERENCES graph_persons (person_id)"
            "\n                DESTINATION KEY (entity_id) REFERENCES graph_entities (entity_id)"
            "\n                LABEL Mentions"
        )

    topic_vertex = ""
    topic_edge = ""
    if has_topics:
        topic_vertex = ",\n            graph_topics  LABEL Topic"
        topic_edge = (
            ",\n            edge_person_topic"
            "\n                SOURCE KEY (person_id) REFERENCES graph_persons (person_id)"
            "\n                DESTINATION KEY (topic_id) REFERENCES graph_topics (topic_id)"
            "\n                LABEL DiscussesTopic"
        )

    sql = f"""
        CREATE OR REPLACE PROPERTY GRAPH comm_graph
        VERTEX TABLES (
            graph_persons LABEL Person,
            graph_groups  LABEL ChatGroup{topic_vertex}{entity_vertex}
        )
        EDGE TABLES (
            edge_person_messaged
                SOURCE KEY (sender_person_id) REFERENCES graph_persons (person_id)
                DESTINATION KEY (receiver_person_id) REFERENCES graph_persons (person_id)
                LABEL Messaged,
            edge_person_in_group
                SOURCE KEY (person_id) REFERENCES graph_persons (person_id)
                DESTINATION KEY (group_jid) REFERENCES graph_groups (group_jid)
                LABEL MemberOf,
            edge_person_cooccurs
                SOURCE KEY (person1_id) REFERENCES graph_persons (person_id)
                DESTINATION KEY (person2_id) REFERENCES graph_persons (person_id)
                LABEL CoOccurs,
            edge_person_conversed
                SOURCE KEY (person1_id) REFERENCES graph_persons (person_id)
                DESTINATION KEY (person2_id) REFERENCES graph_persons (person_id)
                LABEL Conversed{topic_edge}{entity_edge}
        );
    """
    conn.execute(sql)
    log.info("Property graph 'comm_graph' created")


def build_graph(conn: duckdb.DuckDBPyConnection, config: Config) -> dict:
    del config
    t0 = time.time()

    stats: dict[str, int] = {}
    stats.update(build_vertex_tables(conn))
    stats.update(build_edge_tables(conn))
    create_property_graph(conn)
    stats.update(get_graph_stats(conn))

    elapsed = time.time() - t0
    log.info("Graph built in %.1fs", elapsed)
    return stats


def get_graph_stats(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    tables = [
        "graph_persons",
        "graph_groups",
        "graph_topics",
        "graph_entities",
        "edge_person_messaged",
        "edge_person_in_group",
        "edge_person_topic",
        "edge_person_cooccurs",
        "edge_person_conversed",
        "edge_person_mentions_entity",
    ]
    stats = {}
    for t in tables:
        if table_exists(conn, t):
            row = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            if row is None:
                stats[t] = 0
            else:
                stats[t] = int(row[0])
    return stats
