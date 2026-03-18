---
name: wactx
description: >
  Search personal WhatsApp message history using semantic + graph search.
  Use when the user asks "who talked about X", "find conversations about Y",
  "who knows about Z", "who should I reach out to for W",
  or any question about their contacts, groups, or past conversations.
  Returns ranked people with phone numbers, shared groups, and relevant messages.
allowed-tools: Bash(wactx *)
---

# WhatsApp Context Search

Search the user's WhatsApp message history with semantic search + relationship graph.

## Commands

```bash
wactx search "looking for GTM consultants"
wactx search "kubernetes expert" --depth fast        # ~2s, no graph
wactx search "fundraising advice" --depth deep       # ~8s, thorough
wactx search "cofounder" --json                      # machine-readable
wactx search "AI research" --json | jq '.people[:5]' # top 5 people
```

## Depth Presets

| Preset | Queries | Results | Graph | Time |
|--------|---------|---------|-------|------|
| fast | 1 | 10 | no | ~2s |
| balanced | 5 | 15 | yes | ~5s |
| deep | 8 | 30 | yes | ~8s |

## Output Format (--json)

```json
{
  "people": [
    {
      "name": "Person Name",
      "phone": "+919876543210",
      "score": 0.85,
      "dm_volume": 42,
      "shared_groups": ["Group A", "Group B"],
      "top_message": "Their most relevant message..."
    }
  ],
  "messages": [
    {
      "similarity": 0.82,
      "sender": "Person Name",
      "phone": "+919876543210",
      "text": "The actual message...",
      "group": "Group Name",
      "time": "2026-03-01 10:30:00"
    }
  ]
}
```

## When to Use

- "Who has talked about [topic]?"
- "Find me people who know about [skill/domain]"
- "Who should I reach out to for [need]?"
- "What conversations happened about [subject]?"
- "Who are my strongest connections in [area]?"

## Other Useful Commands

```bash
wactx stats                    # database overview
wactx stats --groups           # list all groups with message counts
wactx stats --contacts         # list contacts with message counts
```
