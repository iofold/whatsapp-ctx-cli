from __future__ import annotations

import os

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(width=int(os.environ.get("COLUMNS", 0)) or None)


def _ts(dt) -> str:
    return dt.strftime("%b %d") if hasattr(dt, "strftime") else str(dt)[:10]


def _trunc(s: str, n: int) -> str:
    return s[:n] + "\u2026" if len(s) > n else s


def render_search_results(data: dict) -> None:
    query = data["query"]
    queries_used = data["queries_used"]
    people = data["people"]
    elapsed = data["elapsed"]
    use_graph = data["use_graph"]
    insights = data.get("insights", {})

    progress = data.get("progress", [])

    header_lines: list[str] = []
    header_lines.append(
        f"[bold]\U0001f50d [cyan]{query}[/cyan][/bold]"
        f"  [dim]({len(queries_used)} queries \u00d7 {len(people)} people in {elapsed:.1f}s)[/dim]"
    )
    if queries_used[1:]:
        for q in queries_used[1:]:
            header_lines.append(f"   [dim]+ {q}[/dim]")

    if progress:
        header_lines.append("")
        for i, p in enumerate(progress):
            marker = "\u2713" if i < len(progress) - 1 else "\u25b6"
            header_lines.append(f"  [dim]{marker} {p['label']}[/dim]")

    console.print(
        Panel(
            "\n".join(header_lines),
            title="Search",
            border_style="blue",
        )
    )

    w = console.width or 120

    connection_map: dict[str, dict] = {}
    for c in insights.get("connections", []):
        connection_map[c["name"]] = c

    for i, p in enumerate(people[:15], 1):
        name = p["display_name"]
        phone = p.get("phone", "")
        score = p["score"]
        msgs = p["messages"]

        conn_info = connection_map.get(_trunc(name, 25), {})
        strength = conn_info.get("strength", "")
        indicator = {
            "strong": "\U0001f7e2",
            "weak": "\U0001f7e1",
            "indirect": "\u26aa",
        }.get(strength, "")

        title_parts = [f"[bold]{name}[/bold]"]
        if phone:
            title_parts.append(f"[cyan]{phone}[/cyan]")
        title_parts.append(f"[dim]Score: {score:.2f}[/dim]")
        if indicator:
            title_parts.append(f"{indicator} {conn_info.get('details', '')}")

        title = "  ".join(title_parts)

        lines: list[str] = []

        graph_parts: list[str] = []
        if use_graph:
            community_id = None
            if p.get("messages"):
                community_id = p["messages"][0].get("community_id", -1)
                if community_id is not None and community_id >= 0:
                    graph_parts.append(f"Community #{community_id}")
            if p.get("dm_volume"):
                graph_parts.append(f"{p['dm_volume']} DMs with you")
            if p.get("shared_groups"):
                grps = ", ".join(_trunc(g, 25) for g in p["shared_groups"][:3])
                more = (
                    f" +{len(p['shared_groups']) - 3}"
                    if len(p["shared_groups"]) > 3
                    else ""
                )
                graph_parts.append(f"Groups: {grps}{more}")
            if p.get("entities"):
                ents = ", ".join(v for _, v, _ in p["entities"][:5])
                graph_parts.append(f"Talks about: {ents}")

        if graph_parts:
            lines.append("[dim]" + " · ".join(graph_parts) + "[/dim]")
            lines.append("")

        for j, m in enumerate(msgs[:3]):
            where = m.get("group_name", "")
            ts = _ts(m["time"])
            text = m["text"] or m.get("media_path") or ""
            media = "\U0001f4f7 " if m.get("media_type") else ""
            preview = media + _trunc(text, w - 20)
            lines.append(f"[dim]{ts} {_trunc(where, 30)}:[/dim]  {preview}")

        remaining = p["message_count"] - min(3, len(msgs))
        if remaining > 0:
            lines.append(f"[dim]  ... +{remaining} more messages[/dim]")

        console.print(
            Panel(
                "\n".join(lines),
                title=f"#{i} {title}",
                border_style="green"
                if strength == "strong"
                else "yellow"
                if strength == "weak"
                else "blue",
                width=w,
            )
        )

    topics = insights.get("relevant_topics", [])
    if topics:
        topic_str = "  ".join(
            f"[on grey23] {_trunc(t['entity'], 15)} [/]({t['total_mentions']})"
            for t in topics[:6]
        )
        console.print(f"\n[bold]Relevant topics:[/bold] {topic_str}")

    rels = insights.get("relationships", [])
    if rels:
        rel_strs = []
        for r in rels[:4]:
            groups = (
                f" in {', '.join(_trunc(g, 20) for g in r['groups'][:1])}"
                if r["groups"]
                else ""
            )
            rel_strs.append(
                f"{r['person1']} \u2194 {r['person2']} ({r['exchanges']}x{groups})"
            )
        console.print(f"[bold]Key relationships:[/bold] " + "  \u2502  ".join(rel_strs))

    paths = insights.get("paths", [])
    if paths:
        path_strs = []
        for p in paths[:3]:
            try:
                names = []
                for jid in p["path"]:
                    row_result = None
                    for person in people[:15]:
                        if person.get("sender_jid") == jid:
                            row_result = person["display_name"]
                            break
                    names.append(_trunc(row_result or jid.split("@")[0], 15))
                path_strs.append(f"{' → '.join(names)} (score:{p['score']:.2f})")
            except Exception:
                pass
        if path_strs:
            console.print(f"[bold]Paths:[/bold] " + "  |  ".join(path_strs))


def render_stats(stats: dict) -> None:
    table = Table(title="wactx stats", show_lines=False, width=60)
    table.add_column("Table", style="bold", width=30)
    table.add_column("Rows", justify="right", width=10)

    for name, count in sorted(stats.items()):
        table.add_row(name, f"{count:,}")

    console.print(table)
