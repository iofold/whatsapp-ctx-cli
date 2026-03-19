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


GITHUB_RELEASE_URL = (
    "https://github.com/your-org/whatsapp-ctx-cli/releases/latest/download"
)


def _ensure_sync_binary(cfg: Config) -> None:
    from wactx.sync import find_binary, _PACKAGE_BIN, _platform_binary_name

    if find_binary(cfg):
        click.secho("  ✓ Sync binary found", fg="green")
        return

    click.echo()
    click.echo("  WhatsApp sync binary not found. Building/downloading...")

    import shutil
    import subprocess
    import platform as plat

    go_bin = shutil.which("go")
    if go_bin:
        click.echo("  Go compiler found — building from source...")
        try:
            build_script = Path(__file__).resolve().parent.parent / "build_go.py"
            if build_script.exists():
                subprocess.run(
                    ["uv", "run", "python", str(build_script)],
                    check=True,
                    capture_output=True,
                )
            else:
                _PACKAGE_BIN.mkdir(parents=True, exist_ok=True)
                go_src = Path(__file__).resolve().parent.parent / "whatsapp-sync"
                output = _PACKAGE_BIN / "whatsapp-sync"
                subprocess.run(
                    [go_bin, "build", "-o", str(output), "."],
                    cwd=str(go_src),
                    check=True,
                    capture_output=True,
                    env={**__import__("os").environ, "CGO_ENABLED": "1"},
                )
                output.chmod(0o755)

            if find_binary(cfg):
                click.secho("  ✓ Sync binary built", fg="green")
                return
        except subprocess.CalledProcessError as e:
            click.secho(
                f"  Build failed: {e.stderr.decode()[:200] if e.stderr else e}",
                fg="yellow",
            )

    click.echo("  Trying to download prebuilt binary...")
    try:
        import urllib.request

        bin_name = _platform_binary_name()
        url = f"{GITHUB_RELEASE_URL}/{bin_name}"
        _PACKAGE_BIN.mkdir(parents=True, exist_ok=True)
        dest = _PACKAGE_BIN / "whatsapp-sync"
        urllib.request.urlretrieve(url, dest)
        dest.chmod(0o755)
        if find_binary(cfg):
            click.secho("  ✓ Sync binary downloaded", fg="green")
            return
    except Exception:
        pass

    click.secho("  ⚠ Could not build or download sync binary.", fg="yellow")
    click.echo("  You can build it manually:")
    click.echo("    uv run python build_go.py")
    click.echo("  Or set a custom path:")
    click.echo("    wactx config sync.binary_path /path/to/whatsapp-sync")


PROVIDERS = [
    ("OpenAI", "https://api.openai.com/v1", "text-embedding-3-large", "gpt-4.1-nano"),
    (
        "Cloudflare AI Gateway",
        "https://gateway.ai.cloudflare.com/v1/ACCOUNT_ID/GATEWAY_ID/compat",
        "openai/text-embedding-3-large",
        "openai/gpt-4.1-nano",
    ),
    ("Ollama (local, free)", "http://localhost:11434/v1", "nomic-embed-text", "llama3"),
    ("Custom endpoint", "", "", ""),
]


@cli.command()
@click.pass_context
def init(ctx):
    """Interactive setup: configure provider, API key, and create database."""
    cfg = ctx.obj["config"]

    click.echo()
    click.secho("  wactx — setup", bold=True)
    click.echo()

    click.echo("  Choose your LLM/embedding provider:\n")
    for i, (name, url, _, _) in enumerate(PROVIDERS, 1):
        hint = f"  ({url[:50]}…)" if len(url) > 50 else f"  ({url})" if url else ""
        click.echo(f"    [{i}] {name}{hint}")
    click.echo()

    choice = click.prompt(
        "  Provider", type=click.IntRange(1, len(PROVIDERS)), default=1
    )
    _, base_url, embed_model, chat_model = PROVIDERS[choice - 1]

    if not base_url or "ACCOUNT_ID" in base_url:
        base_url = click.prompt(
            "  API base URL", default=base_url or "https://api.openai.com/v1"
        )

    cfg.api.base_url = base_url

    api_key = click.prompt("  API key", default="", hide_input=False)
    cfg.api.key = api_key

    if embed_model:
        use_default = click.confirm(f"  Embedding model: {embed_model}?", default=True)
        if not use_default:
            embed_model = click.prompt("  Embedding model", default=embed_model)
    else:
        embed_model = click.prompt(
            "  Embedding model", default="text-embedding-3-large"
        )
    cfg.api.embedding_model = embed_model

    if chat_model:
        cfg.api.chat_model = chat_model

    owner = click.prompt(
        "  Your name (for graph insights — who are YOU in the chats)", default=""
    )
    cfg.search.owner_name = owner

    ensure_dirs(cfg)
    save_config(cfg, ctx.obj["config_path"])

    from wactx.db import get_connection, ensure_schema

    conn = get_connection(cfg)
    ensure_schema(conn)
    conn.close()

    click.echo()
    click.secho("  ✓ Config saved", fg="green")
    click.secho(f"  ✓ Database created at {cfg.db_path}", fg="green")

    _ensure_sync_binary(cfg)

    click.echo()
    click.secho("Starting WhatsApp sync...", bold=True)
    click.echo()

    from wactx.sync import sync_whatsapp, find_binary

    if find_binary(cfg):
        sync_whatsapp(cfg, incremental=False, live=False)

        if cfg.api.key:
            click.echo()
            click.secho("Indexing messages...", bold=True)
            from wactx.embed import run_pipeline

            asyncio.run(run_pipeline(cfg))

        click.echo()
        click.secho("  ✓ Setup complete!", fg="green", bold=True)
        click.echo()
        click.echo('  Try: wactx search "your query"')
    else:
        click.echo("  Sync binary not available — skipping initial sync.")
        click.echo()
        click.echo("  Next:")
        click.echo("    wactx sync                        # scan QR, sync, and index")
        click.echo('    wactx search "your query"         # search!')


@cli.command("config")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_cmd(ctx, key, value):
    """Set a config value. Example: wactx config api.key sk-xxx"""
    set_config_value(key, value, ctx.obj["config_path"])
    click.echo(f"Set {key} = {value}")


@cli.command()
@click.option("--full", is_flag=True, help="Full sync instead of incremental")
@click.option(
    "--live", is_flag=True, help="Keep running after sync (receive new messages)"
)
@click.option("--no-index", is_flag=True, help="Skip automatic embedding after sync")
@click.pass_context
def sync(ctx, full, live, no_index):
    """Sync messages from WhatsApp, then embed new messages automatically.

    First run shows a QR code — scan it with WhatsApp on your phone.
    Subsequent runs sync incrementally by default.
    """
    from wactx.sync import sync_whatsapp

    cfg = ctx.obj["config"]
    sync_whatsapp(cfg, incremental=not full, live=live)

    if not no_index and cfg.api.key:
        click.echo()
        click.secho("Indexing new messages...", bold=True)
        from wactx.embed import run_pipeline

        asyncio.run(run_pipeline(cfg))


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


@cli.command()
@click.option("--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
def clean(ctx, yes):
    """Delete database, session, and all local data. Starts fresh."""
    cfg = ctx.obj["config"]
    targets = [
        ("Database", cfg.db_path),
        ("Database WAL", cfg.db_path.parent / (cfg.db_path.name + ".wal")),
        ("WhatsApp session", cfg.db_path.parent / cfg.sync.wa_db_path),
        ("Media directory", cfg.db_path.parent / cfg.sync.media_dir),
    ]

    existing = [(label, p) for label, p in targets if p.exists()]
    if not existing:
        click.echo("Nothing to clean.")
        return

    click.echo("Will delete:")
    for label, p in existing:
        click.echo(f"  {label}: {p}")
    click.echo()

    if not yes and not click.confirm("Are you sure?", default=False):
        click.echo("Aborted.")
        return

    import shutil

    for label, p in existing:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        click.secho(f"  ✓ Deleted {label}", fg="yellow")

    click.echo()
    click.echo("Clean. Run 'wactx init' to start fresh.")


def main():
    cli()
