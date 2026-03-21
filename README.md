# wactx

Semantic and graph search for your WhatsApp messages. All data stays local on your machine. No servers, no cloud, and no telemetry.

**What it does**: Sync your WhatsApp via whatsmeow, embed with any OpenAI-compatible API, build a relationship graph, and search across people, topics, and messages in one query.

Everything is contained in two files:
- `~/.config/wactx/config.toml` (configuration and session)
- `~/.local/share/wactx/messages.duckdb` (database)

## Quickstart

Install `wactx` immediately via pip or uv. No Go compiler or build steps are required.

```bash
pip install whatsapp-ctx-cli
```
or
```bash
uv tool install whatsapp-ctx-cli
```

After installation, the `wactx` command is available in your PATH.

```bash
wactx init                    # interactive setup for provider and API key
wactx sync                    # scan QR code on first run
wactx index                   # embed messages
wactx search "who knows about fundraising"
```

`wactx init` walks you through setting up your API provider and key. It handles the setup automatically.

## Disclaimer

This tool uses whatsmeow, which is an unofficial Go library for the WhatsApp Web multi-device API.

- Usage is **read-only**. This tool only reads messages and never sends or modifies anything.
- Using unofficial WhatsApp APIs carries inherent risk. WhatsApp could restrict or ban accounts that use third-party clients.
- The authors are not responsible for any account actions taken by WhatsApp.
- Use this tool at your own discretion. It is best to test it on non-critical accounts first.

## Data and Privacy

Privacy is the core focus of this project.

- **Local Storage**: All synced messages, contacts, and metadata are stored locally.
- **Database**: Found at `~/.local/share/wactx/messages.duckdb`.
- **Config**: Found at `~/.config/wactx/config.toml`.
- **WhatsApp Session**: The `whatsmeow.db` session file is stored alongside your config.
- **Removal**: To remove all data, run `wactx clean` or manually delete the files listed above.
- **Embeddings**: Message embeddings are sent to the API provider you configure, such as OpenAI or Cloudflare.
- **Zero Data Leak**: If you use a local Ollama instance for embeddings, no data ever leaves your machine.

## Search

```bash
wactx search "kubernetes expert" --depth fast        # ~2s, 1 query, no graph
wactx search "fundraising advice"                    # ~5s, balanced (default)
wactx search "AI research" --depth deep              # ~8s, 8 query variants, full graph

wactx search "cofounder" --variants 3 --top 20
wactx search "sales strategy" --no-graph
wactx search "hiring ML engineers" --json
wactx search "investors" --json | jq '.people[:5]'
```

Search returns two ranked lists:

- **People**: Scored by semantic similarity and graph proximity. This includes DMs, shared groups, and co-mentioned entities.
- **Messages**: Ranked by embedding cosine similarity across multiple query variants.

When graph data is available, you also get a **Graph Insights** panel. This shows shared groups between people, common entities, and your connection strength (🟢 strong, 🟡 weak, or ⚪ indirect).

## Sync and Media

```bash
wactx sync                    # incremental sync (default)
wactx sync --full             # full history sync
wactx sync --live             # stay connected to receive new messages

wactx download                # download all media
wactx download --types image --after 2026-01-01
wactx download --chat "group@g.us"
```

The first run displays a QR code in your terminal. Scan it with the WhatsApp app on your phone to connect. Your session persists across runs.

## Entity Extraction and Graph

```bash
wactx enrich                  # extract persons, orgs, techs, URLs, and events
wactx graph                   # build relationship graph
wactx search "who should I talk to about fundraising"
```

The graph connects:
- **People to People** via DMs and group co-membership.
- **Group memberships** via message activity.
- **Entity mentions** via extracted persons, organizations, technologies, and events.

## Configuration

```toml
# ~/.config/wactx/config.toml

db_path = "~/.local/share/wactx/messages.duckdb"

[api]
base_url = "https://api.openai.com/v1"
key = ""
embedding_model = "text-embedding-3-large"
embedding_dims = 384
chat_model = "gpt-5-mini"
max_concurrent = 5

[sync]
wa_db_path = "whatsmeow.db"
media_dir = "media"
timeout = "5m"

[search]
default_depth = "balanced"
owner_name = "Your Name"
```

The tool works with any OpenAI-compatible endpoint:

```bash
# Cloudflare AI Gateway
wactx config api.base_url https://gateway.ai.cloudflare.com/v1/ACCOUNT/GATEWAY/compat

# Ollama (local and free)
wactx config api.base_url http://localhost:11434/v1
wactx config api.embedding_model nomic-embed-text
```

## All Commands

| Command | Description |
|---------|-------------|
| `wactx init` | Create config and database |
| `wactx config KEY VALUE` | Set a config value |
| `wactx sync` | Sync messages from WhatsApp |
| `wactx download` | Download media attachments |
| `wactx index [--reset]` | Embed messages for semantic search |
| `wactx enrich [--all]` | Extract entities from messages |
| `wactx graph` | Build relationship graph |
| `wactx search QUERY` | Semantic and graph search |
| `wactx stats` | Show database statistics |

## Agent Integration

Copy the skill to your Claude Code skills directory:

```bash
cp -r skills/wactx ~/.claude/skills/
```

Claude will automatically use `wactx search` when you ask about contacts, conversations, or relationships.

## Architecture

```
WhatsApp (phone)
    │
    ▼
wactx sync (bundled Go binary, whatsmeow) ──→ messages + contacts (DuckDB)
    │
    ▼
wactx index  ──→ embeddings via OpenAI-compatible API + HNSW index
    │
    ▼
wactx enrich ──→ extracted entities (persons, orgs, techs, events)
    │
    ▼
wactx graph  ──→ DuckPGQ property graph (vertices + edges)
    │
    ▼
wactx search ──→ multi-query semantic search + graph traversal + rich output
```

Stack: Python, Go, DuckDB, DuckPGQ, DuckDB VSS, whatsmeow, OpenAI-compatible API, Rich, and Click.

## Development

This section is for contributors. It requires [uv](https://docs.astral.sh/uv/) and [Go](https://go.dev/dl/) 1.21+.

```bash
git clone https://github.com/iofold/whatsapp-ctx-cli
cd whatsapp-ctx-cli
uv sync --group dev                     # install dependencies and dev tools
uv run python build_go.py               # compile Go binary
uv run pytest                           # run tests
```

Cross-compile for all platforms:
```bash
uv run python build_go.py --all         # linux/amd64, linux/arm64, darwin/amd64, darwin/arm64, windows/amd64
```

A `Makefile` is included as a convenience, but `uv` is all you need.

## License

MIT
