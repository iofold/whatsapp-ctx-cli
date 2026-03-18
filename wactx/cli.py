from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import click

from wactx.config import Config, load_config, save_config, set_config_value, ensure_dirs


@click.group()
@click.option("--config-path", type=click.Path(), default=None, envvar="WACTX_CONFIG")
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def cli(ctx, config_path, verbose):
    """wactx - Semantic + graph search over your WhatsApp messages."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg_path = Path(config_path) if config_path else None
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(cfg_path)
    ctx.obj["config_path"] = cfg_path


@cli.command()
@click.pass_context
def init(ctx):
    """Initialize wactx: create config file and database."""
    cfg = ctx.obj["config"]
    ensure_dirs(cfg)
    save_config(cfg, ctx.obj["config_path"])

    from wactx.db import get_connection, ensure_schema

    conn = get_connection(cfg)
    ensure_schema(conn)
    conn.close()

    click.echo(f"Config:   {cfg.db_path}")
    click.echo(f"Database: {cfg.db_path}")
    click.echo()
    click.echo("Next steps:")
    click.echo(
        "  1. wactx config api.base_url https://your-openai-compatible-endpoint/v1"
    )
    click.echo("     wactx config api.key your-api-key")
    click.echo()
    click.echo("  2. Sync your WhatsApp messages (recommended):")
    click.echo(
        "     wactx sync                       # connects via QR code on first run"
    )
    click.echo("     OR import a chat export:")
    click.echo("     wactx import chat-export.txt")
    click.echo()
    click.echo("  3. wactx index                      # embed messages")
    click.echo('  4. wactx search "your query"        # search!')


@cli.command("config")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_cmd(ctx, key, value):
    """Set a config value. Example: wactx config api.key sk-xxx"""
    set_config_value(key, value, ctx.obj["config_path"])
    click.echo(f"Set {key} = {value}")


@cli.command("import")
@click.argument("path", type=click.Path(exists=True))
@click.pass_context
def import_cmd(ctx, path):
    """Import a WhatsApp export (.txt or .zip)."""
    from wactx.sync import import_file

    chat_name, count = import_file(ctx.obj["config"], Path(path))
    click.echo(f"Imported {count} messages from '{chat_name}'")


@cli.command()
@click.option("--full", is_flag=True, help="Full sync instead of incremental")
@click.option(
    "--live", is_flag=True, help="Keep running after sync (receive new messages)"
)
@click.pass_context
def sync(ctx, full, live):
    """Sync messages from WhatsApp via whatsmeow (primary method).

    First run shows a QR code — scan it with WhatsApp on your phone.
    Subsequent runs sync incrementally by default.

    Requires the whatsapp-sync binary. Set path with:
      wactx config sync.binary_path /path/to/whatsapp-sync
    """
    from wactx.sync import sync_whatsapp

    sync_whatsapp(ctx.obj["config"], incremental=not full, live=live)


@cli.command()
@click.option("--chat", default=None, help="Filter by chat JID")
@click.option(
    "--types", default="image,video,audio,document", help="Media types to download"
)
@click.option("--after", default=None, help="Only after this date (YYYY-MM-DD)")
@click.option("--before", default=None, help="Only before this date (YYYY-MM-DD)")
@click.pass_context
def download(ctx, chat, types, after, before):
    """Download media attachments from synced messages.

    Requires the whatsapp-sync binary and an active WhatsApp session.
    """
    from wactx.sync import download_media

    download_media(
        ctx.obj["config"], chat=chat, types=types, after=after, before=before
    )


@cli.command()
@click.option("--reset", is_flag=True, help="Re-embed all messages")
@click.pass_context
def index(ctx, reset):
    """Embed messages for semantic search."""
    from wactx.embed import run_pipeline

    asyncio.run(run_pipeline(ctx.obj["config"], reset=reset))


@cli.command()
@click.option("--all", "process_all", is_flag=True, help="Reprocess all messages")
@click.pass_context
def enrich(ctx, process_all):
    """Extract entities (persons, orgs, techs) from messages."""
    from wactx.entities import extract_entities
    from wactx.db import get_connection

    cfg = ctx.obj["config"]
    conn = get_connection(cfg)
    count = asyncio.run(extract_entities(conn, cfg, process_all))
    conn.close()
    click.echo(f"Extracted {count} entity mentions")


@cli.command()
@click.pass_context
def graph(ctx):
    """Build relationship graph (DuckPGQ property graph)."""
    from wactx.graph import build_graph

    counts = build_graph(ctx.obj["config"])
    click.echo("Graph built:")
    for name, count in sorted(counts.items()):
        click.echo(f"  {name:35s} {count:,}")


@cli.command()
@click.argument("query")
@click.option("--depth", type=click.Choice(["fast", "balanced", "deep"]), default=None)
@click.option("--variants", type=int, default=None)
@click.option("--top", type=int, default=None)
@click.option("--no-graph", is_flag=True)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def search(ctx, query, depth, variants, top, no_graph, output_json):
    """Search messages with semantic + graph search."""
    from wactx.search import run_search
    from wactx.db import get_connection

    cfg = ctx.obj["config"]
    depth = depth or cfg.search.default_depth
    conn = get_connection(cfg, read_only=True)

    data = run_search(
        conn, cfg, query, depth=depth, variants=variants, top=top, no_graph=no_graph
    )
    conn.close()

    if output_json:
        out = {
            "query": data["query"],
            "depth": data["depth"],
            "elapsed_s": round(data["elapsed"], 2),
            "queries_used": data["queries_used"],
            "people": [
                {
                    "name": p["display_name"],
                    "phone": p.get("phone", ""),
                    "score": round(p["score"], 3),
                    "similarity": round(p["max_similarity"], 3),
                    "dm_volume": p["dm_volume"],
                    "shared_groups": p["shared_groups"],
                    "entities": [
                        {"type": t, "value": v, "count": c} for t, v, c in p["entities"]
                    ],
                    "message_count": p["message_count"],
                    "top_message": (p["messages"][0]["text"] or "")[:200]
                    if p["messages"]
                    else "",
                }
                for p in data["people"][:20]
            ],
            "messages": [
                {
                    "similarity": round(r["similarity"], 3),
                    "sender": r.get("display_name", r.get("sender")),
                    "phone": r.get("phone", ""),
                    "text": (r["text"] or "")[:200],
                    "group": r.get("group_name"),
                    "time": str(r["time"]),
                }
                for r in data["messages"][:20]
            ],
        }
        click.echo(json.dumps(out, indent=2, default=str))
    else:
        from wactx.render import render_search_results

        render_search_results(data)


@cli.command()
@click.pass_context
def stats(ctx):
    """Show database statistics."""
    from wactx.db import get_connection, get_table_counts
    from wactx.render import render_stats

    cfg = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo("No database found. Run 'wactx init' first.")
        return

    conn = get_connection(cfg, read_only=True)
    counts = get_table_counts(conn)
    conn.close()

    render_stats(counts)


def main():
    cli()
