#!/usr/bin/env python3
"""
Terminal Dashboard for Data Bus — fancy terminal UI for the paper trading data bus.

Shows:
  - Service health & uptime
  - All available endpoints with methods, descriptions, and reachability
  - Cache statistics
  - Recent query log (when --debug is enabled on the bus)
  - Interactive endpoint testing mode

Usage:
  python3 scripts/terminal_dashboard.py                      # auto-refresh dashboard
  python3 scripts/terminal_dashboard.py --test /quotes       # test a specific endpoint
  python3 scripts/terminal_dashboard.py --test-all           # probe all endpoints
  python3 scripts/terminal_dashboard.py --bus http://host:5000  # remote bus

Requires: pip install rich (installed automatically if missing)
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── Rich imports (with graceful fallback) ─────────────────────────────────────
try:
    from rich.live import Live
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.console import Console, Group
    from rich.columns import Columns
    from rich import box
    from rich.align import Align
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    print("ERROR: 'rich' not installed. Run: pip install rich", file=sys.stderr)
    sys.exit(1)


# ── Constants ─────────────────────────────────────────────────────────────────
ENDPOINT_LIST = [
    ("GET",  "/health",               "Service health, uptime, cache stats"),
    ("GET",  "/metrics",              "Prometheus scrape target"),
    ("GET",  "/quotes",               "Live quotes (symbols=AAPL,TSLA)"),
    ("GET",  "/crypto",               "Crypto quotes (symbols=BTC/USD)"),
    ("GET",  "/fundamentals",         "P/E, EPS, market cap (symbol=AAPL)"),
    ("GET,POST","/sentiment",         "FinBERT sentiment (symbol=AAPL)"),
    ("GET",  "/sentiment-divergence", "EN vs ZH sentiment divergence"),
    ("GET",  "/options",              "Options chain snapshot"),
    ("GET",  "/news",                 "Alpaca news headlines"),
    ("GET",  "/news-cache",           "RSS feed from news_cache table"),
    ("GET",  "/news/search",          "Full-text news search (q=query)"),
    ("GET",  "/social",               "Bluesky/Stocktwits/Reddit sentiment"),
    ("GET,POST","/signals",           "Trader intercom reads"),
    ("GET",  "/momentum",             "Cross-sectional momentum rankings"),
    ("GET",  "/macro",                "FRED data, yield curve, FOMC"),
    ("GET",  "/earnings",             "Upcoming earnings calendar"),
    ("GET",  "/flow",                 "Unusual options flow (symbol=AAPL)"),
    ("GET",  "/insiders",             "SEC Form 4 insider filings"),
    ("GET",  "/fear_greed",           "Fear & Greed Index"),
    ("GET",  "/regime",               "Current market regime (ML)"),
    ("GET",  "/overnight-sentiment",  "Overnight sentiment delta"),
    ("GET",  "/technical-scan",       "Multi-timeframe RSI/MACD/BB scan"),
    ("GET",  "/source-quality",       "Prediction accuracy per source"),
    ("GET",  "/risk",                 "Portfolio risk scoring"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def _api_get(bus_url: str, path: str, timeout: float = 5.0) -> dict:
    """GET from the data bus, return parsed JSON or error dict."""
    url = f"{bus_url.rstrip('/')}{path}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.headers.get("Content-Type", "").startswith("text/html"):
                return {"_html": resp.read().decode("utf-8", errors="replace")[:500]}
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "_body": e.read().decode("utf-8", errors="replace")[:200]}
    except urllib.error.URLError as e:
        return {"_error": f"Connection refused: {e.reason}"}
    except json.JSONDecodeError:
        return {"_error": "Invalid JSON response"}
    except Exception as e:
        return {"_error": str(e)}


def _status_icon(ok: bool) -> str:
    return "🟢" if ok else "🔴"


def _method_color(method: str) -> str:
    if "POST" in method:
        return "yellow"
    if "GET" in method:
        return "green"
    return "white"


# ── Dashboard builder ─────────────────────────────────────────────────────────
def build_dashboard(bus_url: str, health: dict, endpoints_ok: dict) -> Layout:
    """Build the full dashboard layout."""
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=1),
    )

    # ── Header ────────────────────────────────────────────────────────────────
    status = Text()
    if health.get("_error"):
        status.append("🔴 DISCONNECTED", style="bold red")
        status.append(f"  — {health['_error']}", style="dim red")
    else:
        status.append("🟢 CONNECTED", style="bold green")
        uptime_s = health.get("uptime_seconds", 0)
        h, m = int(uptime_s // 3600), int((uptime_s % 3600) // 60)
        status.append(f"  |  Uptime: {h}h {m}m", style="dim")
        status.append(f"  |  Service: data-bus", style="dim")
        if health.get("signal_count"):
            status.append(f"  |  Signals: {health['signal_count']}", style="dim cyan")
        if health.get("tracked_symbols"):
            status.append(f"  |  Symbols: {health['tracked_symbols']}", style="dim cyan")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_text = Text()
    header_text.append("📊 Data Bus Terminal Dashboard", style="bold white on blue")
    header_text.append(f"    {bus_url}    ", style="dim")
    header_text.append(f"Refreshed: {now}", style="dim italic")

    layout["header"].update(Panel(Group(header_text, status), box=box.HEAVY))

    # ── Left: Endpoints table ─────────────────────────────────────────────────
    ep_table = Table(title="API Endpoints", box=box.ROUNDED, expand=True,
                     show_header=True, header_style="bold cyan")
    ep_table.add_column("Method", style="bold", width=10, no_wrap=True)
    ep_table.add_column("Path", style="bold white", width=24, no_wrap=True)
    ep_table.add_column("Description", style="dim", width=36)
    ep_table.add_column("Status", width=8, justify="center")

    checked = 0
    ok_count = 0
    for method, path, desc in ENDPOINT_LIST:
        status_icon = "⚪"
        status_color = "dim"
        if path in endpoints_ok:
            checked += 1
            if endpoints_ok[path]:
                status_icon = "✅"
                status_color = "green"
                ok_count += 1
            else:
                status_icon = "❌"
                status_color = "red"
        ep_table.add_row(
            method,
            path,
            desc,
            f"[{status_color}]{status_icon}[/{status_color}]",
        )

    if checked > 0:
        ep_table.caption = f"{ok_count}/{checked} reachable"
    layout["left"].update(Panel(ep_table, title="[bold]📡 Endpoints[/bold]", border_style="blue"))

    # ── Right: Status panels ──────────────────────────────────────────────────
    right_panels = []

    # Cache stats
    cache_info = health.get("cache_stats", {})
    if cache_info and not health.get("_error"):
        cache_text = Text()
        cache_text.append(f"Cache entries: {cache_info.get('keys', '?')}", style="bold green")
        entries = cache_info.get("entries", [])
        if entries:
            cache_text.append("\n\nTop keys:")
            for key in entries[:10]:
                cache_text.append(f"\n  • {key}", style="dim")
        right_panels.append(Panel(cache_text, title="[bold]💾 Cache[/bold]", border_style="green"))

    # Scheduler status
    schedulers = health.get("schedulers", [])
    if schedulers:
        sched_table = Table(box=box.SIMPLE, show_header=True, header_style="bold yellow")
        sched_table.add_column("Source", style="dim", width=12)
        sched_table.add_column("State", width=8)
        for s in schedulers[:8]:
            sched_table.add_row(
                s.get("name", "?"),
                "🟢 running" if s.get("running") else "🔴 stopped",
            )
        right_panels.append(Panel(sched_table, title="[bold]⏱️ Schedulers[/bold]", border_style="yellow"))

    # Quick test reminder
    test_text = Text()
    test_text.append("Quick tests:\n", style="bold")
    test_text.append("  --test /quotes\n", style="dim green")
    test_text.append("  --test /macro\n", style="dim green")
    test_text.append("  --test /signals\n", style="dim green")
    right_panels.append(Panel(test_text, title="[bold]🧪 Test[/bold]", border_style="magenta"))

    layout["right"].update(Panel(Group(*right_panels), title="[bold]📋 Status[/bold]", border_style="blue"))

    # ── Footer ─────────────────────────────────────────────────────────────────
    footer = Text()
    endpoint_count = len(ENDPOINT_LIST)
    if health.get("_error"):
        footer.append(f"⚠️  Data bus unreachable at {bus_url}", style="bold red")
        footer.append(f"  |  {endpoint_count} endpoints known", style="dim")
    else:
        s = "s" if ok_count != 1 else ""
        footer.append(f"✅ {ok_count}/{checked} endpoint{s} reachable", style="bold green")
        footer.append(f"  |  {endpoint_count} total endpoints", style="dim")
    footer.append(f"  |  Press Ctrl+C to exit", style="dim italic")
    layout["footer"].update(Panel(footer, box=box.SIMPLE))

    return layout


def run_dashboard(bus_url: str, refresh_sec: float = 5.0):
    """Run the live-updating dashboard."""
    console = Console()

    def _fetch_state():
        health = _api_get(bus_url, "/health", timeout=3.0)
        endpoints_ok = {}
        # Probe all endpoints with a quick HEAD/GET
        for _, path, _ in ENDPOINT_LIST:
            if path == "/health":
                endpoints_ok[path] = not bool(health.get("_error"))
                continue
            probe = _api_get(bus_url, path, timeout=2.0)
            endpoints_ok[path] = not bool(probe.get("_error"))
        return health, endpoints_ok

    with Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            try:
                health, endpoints_ok = _fetch_state()
                layout = build_dashboard(bus_url, health, endpoints_ok)
                live.update(layout, refresh=True)
            except KeyboardInterrupt:
                break
            except Exception as e:
                live.update(Panel(f"[red]Error: {e}[/red]", title="Error"), refresh=True)
            time.sleep(refresh_sec)


def run_test(bus_url: str, path: str):
    """Test a single endpoint and print formatted result."""
    console = Console()
    console.print(Panel(f"Testing [bold]{bus_url}{path}[/bold]", title="🧪 Endpoint Test"))

    result = _api_get(bus_url, path, timeout=10.0)

    if result.get("_error"):
        if "_body" in result:
            # HTML response — probably a Flask route not found
            console.print(f"[bold red]❌ {result['_error']}[/bold red]")
            if result["_body"].strip():
                console.print(Panel(result["_body"][:500], title="Response body", border_style="red"))
        else:
            console.print(f"[bold red]❌ {result['_error']}[/bold red]")
        return

    # Pretty-print the result
    output = json.dumps(result, indent=2, default=str)
    # Truncate if too large
    if len(output) > 10000:
        output = output[:10000] + f"\n\n... [truncated, {len(output)} chars total]"
    console.print(Panel(output, title="[bold green]✅ Response[/bold green]", border_style="green"))

    # Show summary
    if isinstance(result, dict):
        keys = list(result.keys())[:20]
        console.print(f"[dim]Top-level keys: {', '.join(keys)}[/dim]")
    elif isinstance(result, list):
        console.print(f"[dim]Array with {len(result)} items[/dim]")


def run_test_all(bus_url: str):
    """Probe all endpoints and print a summary table."""
    console = Console()
    table = Table(title=f"Endpoint Probe — {bus_url}", box=box.ROUNDED)
    table.add_column("Method", width=10)
    table.add_column("Path", width=24)
    table.add_column("Status", width=8, justify="center")
    table.add_column("Detail", width=40)

    ok_count = 0
    for method, path, _ in ENDPOINT_LIST:
        result = _api_get(bus_url, path, timeout=5.0)
        if result.get("_error"):
            status = "❌"
            detail = result["_error"]
            style = "red"
        else:
            status = "✅"
            ok_count += 1
            if isinstance(result, dict):
                detail = f"keys: {', '.join(list(result.keys())[:5])}"
                if len(result) > 5:
                    detail += f" (+{len(result) - 5} more)"
            elif isinstance(result, list):
                detail = f"array[{len(result)}]"
            else:
                detail = str(result)[:60]
            style = "green"
        table.add_row(method, path, f"[{style}]{status}[/{style}]", detail)

    total = len(ENDPOINT_LIST)
    table.caption = f"{ok_count}/{total} reachable"
    console.print(table)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Data Bus Terminal Dashboard")
    parser.add_argument("--bus", default="http://localhost:5000",
                        help="Data bus URL (default: http://localhost:5000)")
    parser.add_argument("--test", metavar="PATH",
                        help="Test a specific endpoint (e.g. /quotes)")
    parser.add_argument("--test-all", action="store_true",
                        help="Probe all endpoints")
    parser.add_argument("--refresh", type=float, default=5.0,
                        help="Dashboard refresh interval in seconds (default: 5)")
    args = parser.parse_args()

    if args.test:
        run_test(args.bus, args.test)
    elif args.test_all:
        run_test_all(args.bus)
    else:
        try:
            run_dashboard(args.bus, args.refresh)
        except KeyboardInterrupt:
            print("\n👋 Dashboard closed.")


if __name__ == "__main__":
    main()
