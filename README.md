# wactx

Semantic + graph search over your WhatsApp messages. Built for founders with 100+ groups who need to find people, conversations, and connections fast.

**What it does**: Sync your WhatsApp via whatsmeow → embed with any OpenAI-compatible API → build a relationship graph → search across people, topics, and messages in one query.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and [Go](https://go.dev/dl/) 1.21+.

```bash
git clone https://github.com/your-org/whatsapp-ctx-cli
cd whatsapp-ctx-cli
uv sync
uv run wactx init             # walks you through provider setup, builds Go binary
uv run wactx sync             # scan QR code on first run
uv run wactx index            # embed messages
uv run wactx search "who knows about GTM consultants"
```

`wactx init` is interactive — it asks for your API provider, key, and builds the WhatsApp sync binary automatically. No `make` or manual config needed.

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

- **People** — scored by `0.6 × semantic_similarity + 0.4 × graph_proximity` (DMs, shared groups, co-mentioned entities)
- **Messages** — ranked by embedding cosine similarity across multiple query variants

When graph data is available, you also get a **Graph Insights** panel showing shared groups between result people, common entities, and your connection strength (🟢 strong / 🟡 weak / ⚪ indirect).

## Sync & Media

```bash
wactx sync                    # incremental sync (default)
wactx sync --full             # full history sync
wactx sync --live             # stay connected, receive new messages

wactx download                # download all media
wactx download --types image --after 2026-01-01
wactx download --chat "group@g.us"
```

First run displays a QR code in the terminal — scan it with WhatsApp on your phone. Session persists across runs.

## Entity Extraction & Graph

```bash
wactx enrich                  # extract persons, orgs, techs, URLs, events
wactx graph                   # build relationship graph
wactx search "who should I talk to about fundraising"
```

The graph connects:
- **People ↔ People** via DMs and group co-membership
- **People ↔ Groups** via message activity
- **People ↔ Entities** via extracted mentions (orgs, techs, events)

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

Works with any OpenAI-compatible endpoint:

```bash
# Cloudflare AI Gateway
wactx config api.base_url https://gateway.ai.cloudflare.com/v1/ACCOUNT/GATEWAY/compat

# Ollama (local, free)
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
| `wactx search QUERY` | Semantic + graph search |
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

Stack: Python · Go · DuckDB · DuckPGQ · DuckDB VSS · whatsmeow · OpenAI-compatible API · Rich · Click

## Development

Requires [uv](https://docs.astral.sh/uv/) and [Go](https://go.dev/dl/) 1.21+.

```bash
git clone https://github.com/your-org/whatsapp-ctx-cli
cd whatsapp-ctx-cli
uv sync --group dev                     # install deps + dev tools
uv run python build_go.py               # compile Go binary
uv run pytest                           # run tests
```

Cross-compile for all platforms:
```bash
uv run python build_go.py --all         # linux/amd64, linux/arm64, darwin/amd64, darwin/arm64, windows/amd64
```

A `Makefile` is included as a convenience but `uv` is all you need.

## License

MIT
