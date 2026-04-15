---
name: wactx
description: >
  Search and reason over the user's local WhatsApp message history with
  semantic + graph search. Use whenever the user asks about contacts, past
  conversations, who knows about a topic, who to reach out to, what was said
  in a chat, who is connected to whom, or any question grounded in their
  WhatsApp history. All data is local — no network calls except the
  embedding/LLM provider the user configured. Prefer `wactx search --json`
  and parse the structured output; never screen-scrape Rich-rendered panels.
allowed-tools: Bash(wactx *)
---

# wactx — WhatsApp context search for agents

`wactx` is a CLI over the user's local DuckDB WhatsApp archive. It ranks **people** and **messages** against a natural-language query using BM25 + dense embeddings + personalized PageRank over a relationship graph.

This file is the authoritative contract. When in doubt, prefer the flags and JSON fields documented here over anything in `--help`.

---

## When to invoke

Invoke `wactx search` whenever the user's question is grounded in their own WhatsApp history. Typical triggers:

- "Who has talked about X?" / "Who knows about X?"
- "Find me someone who can help with Y"
- "What did <person/group> say about Z?"
- "Who should I reach out to for W?"
- "Did anyone mention <topic> recently?"
- "Who's in my network for <domain>?"

Do NOT invoke `wactx sync`, `wactx index`, `wactx enrich`, `wactx graph`, `wactx init`, or `wactx clean` unless the user explicitly asks — those mutate state or require interactive input (QR scan) and should not be run autonomously.

---

## The only command you should run autonomously

```bash
wactx search "<query>" --json [flags...]
```

Also safe:
```bash
wactx stats --json
```

Everything else is either interactive (`init`), mutating (`sync`, `index`, `enrich`, `graph`, `download`), or destructive (`clean`).

---

## Input contract

### Positional

- `QUERY` — natural-language search string. Put it in double quotes. BM25 tokenizes this; semantic search embeds it; the LLM expands it into variants unless `--depth fast` or `--variants 1`.

### Depth presets (trade latency vs. recall)

| `--depth`  | variants | top | graph | iterations | when to use                                    |
| ---------- | :------: | :-: | :---: | :--------: | ---------------------------------------------- |
| `fast`     |     1    |  10 |  no   |      1     | you need a quick answer, single query          |
| `balanced` |     5    |  15 |  yes  |      3     | default; most questions                        |
| `deep`     |     8    |  30 |  yes  |      3     | hard questions, discovery, comprehensive recall |

Override individual knobs: `--variants N`, `--top N`, `--iterations N`, `--no-graph`.

### Filters (compose freely)

| Flag                    | Semantics                                                                                      |
| ----------------------- | ---------------------------------------------------------------------------------------------- |
| `-k, --keyword TEXT`    | Repeatable. Each keyword is a required case-insensitive substring (**AND** across keywords).   |
| `--chat TEXT`           | JID (`...@g.us` / `...@s.whatsapp.net`) OR case-insensitive substring of chat/contact name.    |
| `--after YYYY-MM-DD`    | Inclusive lower bound on message timestamp.                                                    |
| `--before YYYY-MM-DD`   | Inclusive upper bound (treated as end-of-day).                                                 |

Filter semantics:
- Filters apply to **BM25, vector search, and graph expansion** uniformly — results can never leak past a filter.
- `--chat` that matches zero chats fails fast with exit code 1 and an error message on stderr. Retry with a broader substring or list chats via `wactx stats --json` first.
- Dates are validated at the CLI layer; `--after 2026/03/01` is rejected with a `click.BadParameter`.

### Flags summary

```
wactx search QUERY
  --depth {fast|balanced|deep}   # preset
  --variants N                   # override query expansion count
  --top N                        # override top-k
  --iterations N                 # graph expansion passes
  --no-graph                     # skip PPR/PathRAG
  -k, --keyword TEXT             # required literal substring (repeat for AND)
  --chat TEXT                    # scope to one chat (JID or name substring)
  --after  YYYY-MM-DD            # time lower bound (inclusive)
  --before YYYY-MM-DD            # time upper bound (inclusive, end of day)
  --json                         # structured output — ALWAYS use this
```

---

## Output contract (`--json`)

Top-level shape:

```json
{
  "query": "kubernetes expert",
  "depth": "balanced",
  "elapsed_s": 0.37,
  "queries_used": ["kubernetes expert", "k8s expert", "..."],
  "progress": [{"label": "Query → 5 variants"}, "..."],
  "filters": {
    "keywords": ["Kubernetes"],
    "chat": "Founders",
    "chat_jids": ["120363abcdef@g.us"],
    "after": "2026-01-01",
    "before": null
  },
  "people":   [ { ...PersonResult   }, ... ],
  "messages": [ { ...MessageResult  }, ... ]
}
```

### `PersonResult`

| Field            | Type               | Meaning                                                                 |
| ---------------- | ------------------ | ----------------------------------------------------------------------- |
| `name`           | string             | Display name (full_name > push_name > JID).                             |
| `phone`          | string             | E.164-ish `+<digits>` derived from JID, or `""` for group members.      |
| `score`          | number 0..1        | Final ranking score (retrieval + PPR + graph + conversational boost).   |
| `similarity`     | number 0..1        | Max cosine similarity of their messages to the query.                   |
| `ppr_score`      | number             | Personalized PageRank score (higher = more central w.r.t. seeds).       |
| `dm_volume`      | integer            | DM exchange count with the owner (if `owner_name` is configured).       |
| `shared_groups`  | string[]           | Groups this person shares with the owner.                               |
| `entities`       | `{type,value,count}[]` | Top entities this person talks about (tech / org / event / url / person). |
| `message_count`  | integer            | How many of this person's messages made it into the top results.        |
| `top_message`    | string (≤200 chars) | Their single most relevant message, truncated.                         |

### `MessageResult`

| Field        | Type    | Meaning                                   |
| ------------ | ------- | ----------------------------------------- |
| `similarity` | number  | Cosine similarity to the query embedding. |
| `sender`     | string  | Display name of the sender.               |
| `phone`      | string  | E.164-ish phone of the sender.            |
| `text`       | string  | Message text, ≤200 chars.                 |
| `group`      | string  | Chat label (group name or `"DM"`).        |
| `time`       | string  | Timestamp (string-cast).                  |

### People are the answer to "who"; messages are the evidence

The usual recipe:

1. Run `wactx search "..." --json` with the user's question.
2. Surface the top 3–5 `people[]` with name, phone, and 1–2 quoted messages each (from that person's `top_message` or the matching entries in `messages[]`).
3. Pull graph signals (`dm_volume`, `shared_groups`, `entities`) as context so the user can decide *who to actually contact*.
4. Always include the phone number — that's the primary action the user will take.

---

## Exit codes and errors

| Exit | Meaning                                                                       |
| ---: | ----------------------------------------------------------------------------- |
|  `0` | Success (including zero results — check `len(people)` / `len(messages)`).     |
|  `1` | Error: invalid date, unknown chat filter, missing database, CLI usage error.  |

Errors go to **stderr**; `--json` output is on **stdout**. Parse only stdout. On non-zero exit, check stderr for a `click`-style message.

Known error shapes:

- `No chats match: 'foo'` → your `--chat` substring didn't match. Try a broader substring, or run `wactx stats --json` and look at the tables to pick a JID directly.
- `--after must be YYYY-MM-DD, got '...'` → reformat the date. Only `YYYY-MM-DD` is accepted.
- `No database found. Run 'wactx init' first.` → the user hasn't set up wactx. Tell them, don't try to run `init` yourself (it's interactive).

---

## Recipes

### "Who knows about Kubernetes?"

```bash
wactx search "kubernetes expert" --json
```

Parse `people[:5]`, name + phone + top_message.

### "Who's talked about fundraising recently?"

```bash
wactx search "fundraising advice" --after 2026-01-01 --json
```

### "What did anyone say about GPT-5 in the Founders group?"

```bash
wactx search "GPT-5 impressions" --chat "Founders" --json
```

### "Show me any message that literally mentions Claude Code"

```bash
wactx search "anything" -k "Claude Code" --depth fast --json
```

The `-k` filter ensures literal substring match; `"anything"` is a weak query that lets BM25/vector ranking fall back to the keyword filter.

### "Who do I know at OpenAI?"

```bash
wactx search "works at OpenAI" -k OpenAI --json
```

### "Fundraising advice in ML Engineers group, March 2026"

```bash
wactx search "seed round" \
  --chat "ML Engineers" \
  --after 2026-03-01 --before 2026-03-31 \
  --json
```

### Reducing output size for large responses

Agents should pipe through `jq` to trim tokens:

```bash
wactx search "kubernetes" --json | jq '{
  people: [.people[] | {name, phone, score, top_message}][:5],
  messages: [.messages[] | {sender, text, time}][:5]
}'
```

Prefer this over dumping the full JSON into the context window.

### Discovering what's in the database

```bash
wactx stats --json
# {
#   "db_path": "/home/.../messages.duckdb",
#   "tables": { "messages": 42817, "contacts": 531, ... }
# }
```

---

## Agent-specific rules

**DO:**
- Always pass `--json` and parse the result.
- Always quote the `QUERY` argument.
- Compose filters instead of post-filtering the JSON — filters are applied server-side across BM25, vector, and graph passes.
- Prefer narrow filters (`--chat`, date range) on follow-up questions so the context window stays small.
- Trim output with `jq` before presenting to the user or re-feeding into reasoning.
- Report `people[].phone` whenever you tell the user "contact X" — that's the actionable identifier.

**DO NOT:**
- Do NOT run `wactx init`, `wactx sync`, `wactx clean`, `wactx index`, `wactx enrich`, `wactx graph`, or `wactx download` without an explicit user request. These mutate state, require a QR scan, or make API calls that cost the user money.
- Do NOT screen-scrape the Rich-rendered (non-`--json`) output. It is not stable.
- Do NOT use `--keyword` for loose thematic matches — BM25 + semantic already handle that. Use `-k` only when you need *literal* substring containment (for example, a company name, a URL fragment, an unusual acronym, or when the user says "messages that mention X").
- Do NOT invent JIDs. Either pass a substring of a chat name to `--chat`, or pull a real JID from a prior `wactx search --json` response / `wactx stats --json`.
- Do NOT pass dates in any format other than `YYYY-MM-DD`. No relative dates (`"last week"`), no slashes, no timezones.

---

## Failure-mode cheatsheet

| Symptom                                                   | Cause                                                     | Fix                                                                  |
| --------------------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------- |
| Empty `people[]` and `messages[]`                         | Filters too narrow, or DB not yet indexed                 | Drop a filter; run `wactx stats --json` to confirm the DB has rows.  |
| Exit 1, stderr says "No chats match"                      | `--chat` substring hit zero rows                          | Try a shorter substring, or pass a JID directly.                     |
| Exit 1, stderr says "--after must be YYYY-MM-DD"          | Wrong date format                                         | Reformat to `YYYY-MM-DD`.                                            |
| `queries_used` is length 1 on a `--depth balanced` query  | Query expansion LLM call failed (API key, network)        | Tell the user their provider might be down; try again or `--depth fast`. |
| `score` is very low on every result                       | Query is off-topic for this user's archive                | Rephrase, drop `-k`, or broaden the date range.                      |

---

## Bounds and cost

- `wactx search` **reads** the DB. It does not mutate anything. Safe to call repeatedly.
- `wactx search` **calls the user's embedding + chat provider** (OpenAI by default) to expand queries and embed them. Each call is small but non-zero cost. Prefer `--depth fast` when a fast single-query answer is enough.
- `wactx search` opens the DuckDB file in read-only mode — you can run it in parallel with other `wactx search` calls safely.
