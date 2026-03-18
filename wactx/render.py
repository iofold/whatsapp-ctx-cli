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
    results = data["messages"]
    elapsed = data["elapsed"]
    use_graph = data["use_graph"]
    insights = data.get("insights", {})

    header = Text()
    header.append("\U0001f50d ", style="bold")
    header.append(query, style="bold cyan")
    header.append(
        f"  ({len(queries_used)} queries \u00d7 {len(results)} results in {elapsed:.1f}s)"
    )
    if queries_used[1:]:
        header.append("\n   + " + "\n   + ".join(queries_used[1:]), style="dim")
    console.print(Panel(header, title="Search", border_style="blue"))

    w = console.width or 120
    msg_w = max(40, w - 42)

    ptable = Table(title="\U0001f464 People", show_lines=True, width=w, pad_edge=False)
    ptable.add_column("#", style="dim", width=2, no_wrap=True)
    ptable.add_column("Contact", width=30, no_wrap=True, overflow="ellipsis")
    ptable.add_column("Message", width=msg_w, no_wrap=True, overflow="ellipsis")

    for i, p in enumerate(people[:15], 1):
        best = p["messages"][0]
        name = _trunc(p["display_name"], 28)
        phone = p.get("phone", "")
        where = best.get("group_name", "")
        ts = _ts(best["time"])
        preview = _trunc((best["text"] or best.get("media_path") or ""), msg_w - 2)

        meta_parts = [
            f"Scr:{p['score']:.2f}",
            f"Sim:.{int(p['max_similarity'] * 1000):03d}",
        ]
        if use_graph:
            if p["dm_volume"]:
                meta_parts.append(f"DMs:{p['dm_volume']}")
            if p["shared_groups"]:
                meta_parts.append(f"Grp:{len(p['shared_groups'])}")
            if p["entities"]:
                ents = ", ".join(v for _, v, _ in p["entities"][:3])
                meta_parts.append(f"\U0001f3f7 {ents}")
        meta_parts.append(f"Msgs:{p['message_count']}")

        contact_cell = f"[bold]{name}[/bold]\n[cyan]{phone}[/cyan]  [dim]{' \u00b7 '.join(meta_parts)}[/dim]"
        msg_cell = f"[dim]{ts} {where}:[/dim] {preview}\n"
        if use_graph and p["shared_groups"]:
            grps = ", ".join(_trunc(g, 22) for g in p["shared_groups"][:3])
            msg_cell += f"[dim]  \u2514 groups: {grps}[/dim]"

        ptable.add_row(str(i), contact_cell, msg_cell)

    console.print(ptable)

    if use_graph and insights:
        _render_graph_insights(insights, w)

    mtable = Table(
        title="\U0001f4ac Messages", show_lines=True, width=w, pad_edge=False
    )
    mtable.add_column("#", style="dim", width=2, no_wrap=True)
    mtable.add_column("Contact", width=30, no_wrap=True, overflow="ellipsis")
    mtable.add_column("Message", width=msg_w, no_wrap=True, overflow="ellipsis")

    for i, r in enumerate(results[:15], 1):
        sender = r.get("display_name", r.get("sender", "?"))
        phone = r.get("phone", "")
        where = _trunc(r.get("group_name", ""), 22)
        media = "\U0001f4f7 " if r.get("media_type") else ""
        text = media + _trunc((r["text"] or r.get("media_path") or ""), msg_w - 2)
        dm = f"  DMs:{r['dm_volume']}" if r.get("dm_volume") else ""

        contact_cell = f"{_trunc(sender, 28)}\n[cyan]{phone}[/cyan]{dm}"
        msg_cell = f"[dim]{_ts(r['time'])} {where}:[/dim]\n{text}"
        mtable.add_row(str(i), contact_cell, msg_cell)

    console.print(mtable)


def _render_graph_insights(insights: dict, width: int) -> None:
    lines: list[str] = []

    if insights.get("shared_groups"):
        lines.append("[bold]\U0001f517 Shared Groups Between Results[/bold]")
        for sg in insights["shared_groups"][:8]:
            grps = ", ".join(_trunc(g, 25) for g in sg["groups"][:3])
            more = f" +{len(sg['groups']) - 3}" if len(sg["groups"]) > 3 else ""
            lines.append(
                f"  {sg['person1']} \u2194 {sg['person2']}  via [dim]{grps}{more}[/dim]"
            )

    if insights.get("common_entities"):
        lines.append("")
        lines.append("[bold]\U0001f3f7  Common Entities Across Results[/bold]")
        for ce in insights["common_entities"][:6]:
            who = ", ".join(ce["people"][:4])
            lines.append(f"  [on grey23] {ce['entity']} [/]  mentioned by {who}")

    if insights.get("connections"):
        lines.append("")
        lines.append("[bold]\U0001f4ca Your Connection to Results[/bold]")
        for c in insights["connections"]:
            indicator = {
                "strong": "\U0001f7e2",
                "weak": "\U0001f7e1",
                "indirect": "\u26aa",
            }.get(c["strength"], "\u26aa")
            lines.append(f"  {indicator} {c['name']:20s}  {c['details']}")

    if lines:
        console.print(
            Panel(
                "\n".join(lines),
                title="\U0001f9e0 Graph Insights",
                border_style="green",
                width=width,
            )
        )


def render_stats(stats: dict) -> None:
    table = Table(title="wactx stats", show_lines=False, width=60)
    table.add_column("Table", style="bold", width=30)
    table.add_column("Rows", justify="right", width=10)

    for name, count in sorted(stats.items()):
        table.add_row(name, f"{count:,}")

    console.print(table)
