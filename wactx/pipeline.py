from __future__ import annotations

import asyncio
import logging
import time

import click

from wactx.config import Config

log = logging.getLogger("wactx.pipeline")


async def _run_processing(config: Config) -> tuple[int, int]:
    from wactx.embed import run_pipeline
    from wactx.entities import extract_entities
    from wactx.db import get_connection

    embedded = 0
    try:
        result = await run_pipeline(config)
        embedded = int(result or 0)
    except Exception as e:
        log.warning("Indexing failed: %s", e)

    entities = 0
    conn = get_connection(config)
    try:
        entities = int(await extract_entities(conn, config, process_all=False) or 0)
    except Exception as e:
        log.warning("Entity extraction failed: %s", e)
    finally:
        conn.close()

    return embedded, entities


def run_post_sync(config: Config) -> None:
    t0 = time.time()

    click.echo()
    click.secho("Post-sync processing...", bold=True)

    click.echo("  Indexing messages + extracting entities...")
    embedded, entities = asyncio.run(_run_processing(config))
    click.secho(
        f"  ✓ Indexed {embedded} messages, extracted {entities} entity mentions",
        fg="green",
    )

    click.echo("  Building relationship graph...")
    from wactx.graph import build_graph
    from wactx.db import get_connection

    conn = get_connection(config)
    try:
        stats = build_graph(conn, config)
        persons = stats.get("graph_persons", 0)
        edges = sum(v for k, v in stats.items() if k.startswith("edge_"))
        click.secho(
            f"  ✓ Graph built: {persons} people, {edges} connections", fg="green"
        )
    except Exception as e:
        log.warning("Graph build failed: %s", e)
        click.secho(f"  ⚠ Graph build failed: {e}", fg="yellow")
    finally:
        conn.close()

    elapsed = time.time() - t0
    click.secho(f"  ✓ Post-sync complete ({elapsed:.1f}s)", fg="green")
