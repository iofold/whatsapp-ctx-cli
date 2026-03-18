# wactx

Semantic + graph search over your WhatsApp messages. Built for founders with 100+ groups who need to find people, conversations, and connections fast.

**What it does**: Sync your WhatsApp via whatsmeow → embed with any OpenAI-compatible API → build a relationship graph → search across people, topics, and messages in one query.

## Quickstart

```bash
pip install whatsapp-ctx-cli

# 1. Initialize
wactx init

# 2. Configure your embedding/LLM provider (any OpenAI-compatible endpoint)
wactx config api.base_url https://api.openai.com/v1
wactx config api.key sk-your-key-here

# 3. Sync your WhatsApp messages (recommended)
wactx config sync.binary_path /path/to/whatsapp-sync
wactx sync                    # shows QR code on first run — scan with your phone

# 4. Build the search index
wactx index

# 5. Search
wactx search "who knows about GTM consultants"
```

### Alternative: Import from export file

If you don't have the whatsapp-sync binary, you can import WhatsApp's built-in export:

```bash
# WhatsApp → Chat → Export Chat → Without Media → save .txt or .zip
wactx import "WhatsApp Chat with Founders Group.txt"
```

## Search

```bash
# Depth presets control speed vs thoroughness
wactx search "kubernetes expert" --depth fast        # ~2s, 1 query, no graph
wactx search "fundraising advice"                    # ~5s, balanced (default)
wactx search "AI research" --depth deep              # ~8s, 8 query variants, full graph

# Fine-tune
wactx search "cofounder" --variants 3 --top 20
wactx search "sales strategy" --no-graph             # skip graph enrichment

# Machine-readable output
wactx search "hiring ML engineers" --json
wactx search "investors" --json | jq '.people[:5]'
```

Search returns two ranked lists:

- **People** — scored by `0.6 × semantic_similarity + 0.4 × graph_proximity` (DMs, shared groups, co-mentioned entities)
- **Messages** — ranked by embedding cosine similarity across multiple query variants

When graph data is available, you also get a **Graph Insights** panel showing shared groups between result people, common entities, and your connection strength (🟢 strong / 🟡 weak / ⚪ indirect).

## Entity Extraction & Graph

```bash
# Extract persons, orgs, technologies, URLs, events from group messages
wactx enrich

# Build the relationship graph (DuckPGQ property graph)
wactx graph

# Now search includes graph context automatically
wactx search "who should I talk to about fundraising"
```

The graph connects:
- **People ↔ People** via DMs and group co-membership
- **People ↔ Groups** via message activity
- **People ↔ Entities** via extracted mentions (orgs, techs, events)
- **People ↔ Topics** via message classifications

## Configuration

Config lives at `~/.config/wactx/config.toml`:

```toml
[database]
path = "~/.local/share/wactx/messages.duckdb"

[api]
base_url = "https://api.openai.com/v1"
key = ""
embedding_model = "text-embedding-3-large"
embedding_dims = 384
chat_model = "gpt-4.1-mini"
max_concurrent = 5

[search]
default_depth = "balanced"
owner_name = "Your Name"          # enables "Your Connection" in graph insights
```

Set values via CLI:

```bash
wactx config api.base_url https://your-endpoint/v1
wactx config api.key your-key
wactx config search.owner_name "Your Name"
```

### Provider Examples

Works with any OpenAI-compatible endpoint:

```bash
# OpenAI direct
wactx config api.base_url https://api.openai.com/v1

# Cloudflare AI Gateway
wactx config api.base_url https://gateway.ai.cloudflare.com/v1/ACCOUNT/GATEWAY/compat

# Ollama (local, free)
wactx config api.base_url http://localhost:11434/v1
wactx config api.embedding_model nomic-embed-text

# vLLM / any OpenAI-compatible server
wactx config api.base_url http://your-server:8000/v1
```

## All Commands

| Command | Description |
|---------|-------------|
| `wactx init` | Create config file and database |
| `wactx config KEY VALUE` | Set a config value |
| `wactx sync [--full] [--live]` | **Sync from WhatsApp** via whatsmeow (primary method) |
| `wactx download [--chat JID]` | Download media attachments |
| `wactx import FILE` | Import WhatsApp export (.txt or .zip) — fallback |
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

Claude will automatically use `wactx search` when you ask about contacts, conversations, or relationships. All search results are also available as JSON:

```bash
wactx search "AI engineers in my network" --json
```

## Architecture

```
WhatsApp (phone)                      WhatsApp Export (.txt/.zip)
    │                                         │
    ▼                                         ▼
wactx sync (whatsmeow) ──→ messages + contacts tables (DuckDB) ←── wactx import
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

Stack: Python · DuckDB · DuckPGQ · DuckDB VSS · OpenAI-compatible API · Rich · Click

## Development

```bash
git clone https://github.com/your-org/whatsapp-ctx-cli
cd whatsapp-ctx-cli
pip install -e ".[dev]"
pytest
```

## License

MIT
