from __future__ import annotations

import logging
import time

import duckdb

from wactx.config import Config
from wactx.db import table_exists

log = logging.getLogger("wactx.graph")


def _run(conn: duckdb.DuckDBPyConnection, label: str, sql: str) -> int:
    conn.execute(sql)
    table = sql.split("TABLE")[1].split("AS")[0].strip().split()[-1]
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    log.info("  %-35s %d rows", label, count)
    return count


def build_vertex_tables(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    log.info("Building vertex tables...")
    counts = {}

    counts["graph_persons"] = _run(
        conn,
        "graph_persons",
        """
        CREATE OR REPLACE TABLE graph_persons AS
        SELECT ROW_NUMBER() OVER () AS person_id,
               jid AS source_id, 'whatsapp' AS source,
               COALESCE(push_name, full_name, business_name, jid) AS display_name,
               push_name, full_name, CAST(NULL AS VARCHAR) AS email_address
        FROM contacts WHERE is_group = false
    """,
    )

    counts["graph_groups"] = _run(
        conn,
        "graph_groups",
        """
        CREATE OR REPLACE TABLE graph_groups AS
        SELECT jid AS group_jid,
               COALESCE(group_name, push_name, jid) AS group_name,
               (SELECT COUNT(DISTINCT sender_jid) FROM messages m
                WHERE m.chat_jid = c.jid AND m.is_group) AS member_count
        FROM contacts c WHERE is_group = true
    """,
    )

    counts["graph_topics"] = (
        _run(
            conn,
            "graph_topics",
            """
        CREATE OR REPLACE TABLE graph_topics AS
        SELECT ROW_NUMBER() OVER () AS topic_id, category AS topic_name,
               'classification' AS source, COUNT(*) AS message_count
        FROM classifications GROUP BY category HAVING COUNT(*) >= 5
    """,
        )
        if table_exists(conn, "classifications")
        else 0
    )

    if table_exists(conn, "extracted_entities"):
        counts["graph_entities"] = _run(
            conn,
            "graph_entities",
            """
            CREATE OR REPLACE TABLE graph_entities AS
            SELECT ROW_NUMBER() OVER () AS entity_id,
                   entity_type, entity_value, COUNT(*) AS mention_count
            FROM extracted_entities
            GROUP BY entity_type, entity_value HAVING COUNT(*) >= 2
        """,
        )

    return counts


def build_edge_tables(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    log.info("Building edge tables...")
    counts = {}

    counts["edge_person_messaged"] = _run(
        conn,
        "edge_person_messaged",
        """
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
        GROUP BY gp_sender.person_id, gp_receiver.person_id
    """,
    )

    counts["edge_person_in_group"] = _run(
        conn,
        "edge_person_in_group",
        """
        CREATE OR REPLACE TABLE edge_person_in_group AS
        SELECT ROW_NUMBER() OVER () AS edge_id, gp.person_id, gg.group_jid,
               COUNT(*) AS message_count,
               MIN(m.timestamp) AS first_seen, MAX(m.timestamp) AS last_seen
        FROM messages m
        JOIN graph_persons gp ON m.sender_jid = gp.source_id
        JOIN graph_groups gg ON m.chat_jid = gg.group_jid
        WHERE m.is_group = true
        GROUP BY gp.person_id, gg.group_jid
    """,
    )

    if table_exists(conn, "graph_topics"):
        counts["edge_person_topic"] = _run(
            conn,
            "edge_person_topic",
            """
            CREATE OR REPLACE TABLE edge_person_topic AS
            SELECT ROW_NUMBER() OVER () AS edge_id, gp.person_id, gt.topic_id,
                   COUNT(*) AS mention_count
            FROM messages m
            JOIN classifications cl ON m.id = cl.message_id AND m.chat_jid = cl.chat_jid
            JOIN graph_persons gp ON m.sender_jid = gp.source_id
            JOIN graph_topics gt ON cl.category = gt.topic_name
            GROUP BY gp.person_id, gt.topic_id
        """,
        )

    counts["edge_person_cooccurs"] = _run(
        conn,
        "edge_person_cooccurs",
        """
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
        GROUP BY a.person_id, b.person_id
    """,
    )

    if table_exists(conn, "graph_entities"):
        counts["edge_person_mentions_entity"] = _run(
            conn,
            "edge_person_mentions_entity",
            """
            CREATE OR REPLACE TABLE edge_person_mentions_entity AS
            SELECT ROW_NUMBER() OVER () AS edge_id,
                   gp.person_id, ge.entity_id, COUNT(*) AS mention_count
            FROM extracted_entities ee
            JOIN messages m ON ee.message_id = m.id
            JOIN graph_persons gp ON m.sender_jid = gp.source_id
            JOIN graph_entities ge ON ee.entity_type = ge.entity_type AND ee.entity_value = ge.entity_value
            GROUP BY gp.person_id, ge.entity_id
        """,
        )

    return counts


def create_property_graph(conn: duckdb.DuckDBPyConnection) -> None:
    has_entities = table_exists(conn, "graph_entities") and table_exists(
        conn, "edge_person_mentions_entity"
    )
    has_topics = table_exists(conn, "graph_topics") and table_exists(
        conn, "edge_person_topic"
    )

    entity_v = ",\n            graph_entities LABEL Entity" if has_entities else ""
    topic_v = ",\n            graph_topics  LABEL Topic" if has_topics else ""
    topic_e = ""
    if has_topics:
        topic_e = """,
            edge_person_topic
                SOURCE KEY (person_id) REFERENCES graph_persons (person_id)
                DESTINATION KEY (topic_id) REFERENCES graph_topics (topic_id)
                LABEL DiscussesTopic"""
    entity_e = ""
    if has_entities:
        entity_e = """,
            edge_person_mentions_entity
                SOURCE KEY (person_id) REFERENCES graph_persons (person_id)
                DESTINATION KEY (entity_id) REFERENCES graph_entities (entity_id)
                LABEL Mentions"""

    sql = f"""
        CREATE OR REPLACE PROPERTY GRAPH comm_graph
        VERTEX TABLES (
            graph_persons LABEL Person,
            graph_groups  LABEL ChatGroup{topic_v}{entity_v}
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
                LABEL CoOccurs{topic_e}{entity_e}
        );
    """
    conn.execute(sql)
    log.info("Property graph 'comm_graph' created")


def build_graph(config: Config) -> dict[str, int]:
    from wactx.db import get_connection

    conn = get_connection(config)
    t0 = time.time()

    conn.execute("INSTALL duckpgq FROM community; LOAD duckpgq")

    all_counts = {}
    all_counts.update(build_vertex_tables(conn))
    all_counts.update(build_edge_tables(conn))
    create_property_graph(conn)

    elapsed = time.time() - t0
    log.info("Graph built in %.1fs", elapsed)
    conn.close()
    return all_counts


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
        "edge_person_mentions_entity",
    ]
    stats = {}
    for t in tables:
        if table_exists(conn, t):
            stats[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return stats
