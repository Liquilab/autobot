"""
Research Loop v2 - Simplified learning for the trading bot.

v2 changes (after quant review):
- Focus on per-source P&L tracking (the only thing that matters with <50 trades)
- Drop complex calibration-based parameter tuning (noise with small N)
- Binary decisions: after 20 trades per source, block sources with negative ROI
- Run every 2 hours as safety net (quick_learn runs per-trade in the bot)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

log = logging.getLogger("autobot")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STRATEGY_FILE = DATA_DIR / "strategy_params.json"
PNL_FILE = DATA_DIR / "pnl.json"
TRADES_FILE = DATA_DIR / "trades.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
RESEARCH_DIR = BASE_DIR / "reports" / "research"


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_strategy_params():
    try:
        with open(STRATEGY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return get_default_params()


def get_default_params():
    return {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source_stats": {},
        "blocked_sources": [],
        "position_sizing": {
            "max_fraction": 0.15,
            "kelly_multiplier": 0.5,
        },
        "total_resolved_trades": 0,
        "overall_roi": 0.0,
    }


def analyze_by_source(pnl: list, trades: list) -> dict:
    """Analyze P&L broken down by signal source."""
    # Build trade lookup for signal_source
    trade_lookup = {}
    for t in trades:
        key = t.get("market", "")
        trade_lookup[key] = t

    source_stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0,
        "total_cost": 0.0, "total_profit": 0.0,
    })

    for record in pnl:
        market = record.get("market", "")
        # Get source from PnL record first, then from trade lookup
        source = record.get("signal_source", "")
        if not source:
            trade = trade_lookup.get(market, {})
            source = trade.get("signal_source", "heuristic")

        s = source_stats[source]
        s["trades"] += 1
        won = record.get("won", False)
        s["wins"] += 1 if won else 0
        s["losses"] += 0 if won else 1
        s["total_cost"] += record.get("cost", 0)
        s["total_profit"] += record.get("profit", 0)

    # Compute derived stats
    result = {}
    for source, s in source_stats.items():
        n = s["trades"]
        result[source] = {
            "trades": n,
            "wins": s["wins"],
            "losses": s["losses"],
            "total_cost": round(s["total_cost"], 2),
            "total_profit": round(s["total_profit"], 2),
            "win_rate": round(s["wins"] / n, 3) if n > 0 else 0,
            "roi": round(s["total_profit"] / s["total_cost"], 4) if s["total_cost"] > 0 else 0,
        }

    return result


def analyze_by_category(pnl: list, trades: list) -> dict:
    """Simple category breakdown."""
    trade_lookup = {}
    for t in trades:
        key = t.get("market", "")
        trade_lookup[key] = t

    cat_stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0,
        "total_cost": 0.0, "total_profit": 0.0,
    })

    for record in pnl:
        market = record.get("market", "")
        trade = trade_lookup.get(market, {})
        category = trade.get("category", "unknown")

        s = cat_stats[category]
        s["trades"] += 1
        won = record.get("won", False)
        s["wins"] += 1 if won else 0
        s["losses"] += 0 if won else 1
        s["total_cost"] += record.get("cost", 0)
        s["total_profit"] += record.get("profit", 0)

    result = {}
    for cat, s in cat_stats.items():
        n = s["trades"]
        result[cat] = {
            "trades": n,
            "wins": s["wins"],
            "losses": s["losses"],
            "total_cost": round(s["total_cost"], 2),
            "total_profit": round(s["total_profit"], 2),
            "win_rate": round(s["wins"] / n, 3) if n > 0 else 0,
            "roi": round(s["total_profit"] / s["total_cost"], 4) if s["total_cost"] > 0 else 0,
        }

    return result


def update_strategy_params(source_stats: dict, current_params: dict) -> tuple[dict, list]:
    """
    Simple learning: block bad sources, keep good ones.
    No micro-adjustments of edge parameters (noise with small N).
    """
    new_params = json.loads(json.dumps(current_params))
    insights = []

    # Update source_stats in params
    new_params["source_stats"] = {}
    blocked = new_params.get("blocked_sources", [])

    total_trades = 0
    total_cost = 0.0
    total_profit = 0.0

    for source, stats in source_stats.items():
        new_params["source_stats"][source] = stats
        total_trades += stats["trades"]
        total_cost += stats["total_cost"]
        total_profit += stats["total_profit"]

        n = stats["trades"]
        roi = stats["roi"]
        win_rate = stats["win_rate"]

        if n >= 20 and roi < 0 and source not in blocked:
            blocked.append(source)
            insights.append(
                f"GEBLOKKEERD: '{source}' na {n} trades, "
                f"ROI: {roi*100:.1f}%, win rate: {win_rate*100:.0f}%"
            )
        elif n >= 10:
            status = "WINSTGEVEND" if roi > 0 else "VERLIESGEVEND"
            insights.append(
                f"{status}: '{source}' - {n} trades, "
                f"ROI: {roi*100:.1f}%, win rate: {win_rate*100:.0f}%"
            )
        elif n >= 3:
            insights.append(
                f"LEREN: '{source}' - {n} trades (min 20 voor beslissing), "
                f"ROI: {roi*100:.1f}%, win rate: {win_rate*100:.0f}%"
            )

    new_params["blocked_sources"] = blocked
    new_params["total_resolved_trades"] = total_trades
    new_params["overall_roi"] = round(total_profit / total_cost, 4) if total_cost > 0 else 0
    new_params["version"] = current_params.get("version", 0) + 1
    new_params["updated_at"] = datetime.now(timezone.utc).isoformat()

    if total_trades > 0:
        overall_win_rate = sum(s["wins"] for s in source_stats.values()) / total_trades
        insights.append(
            f"OVERALL: {total_trades} trades, "
            f"ROI: {new_params['overall_roi']*100:.1f}%, "
            f"win rate: {overall_win_rate*100:.0f}%, "
            f"P&L: ${total_profit:+.2f}"
        )

    return new_params, insights


def write_research_note(source_stats: dict, category_stats: dict,
                        insights: list, new_params: dict):
    """Write a research note in Dutch."""
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    filename = now.strftime("research-%Y-%m-%d-%H-%M.md")
    filepath = RESEARCH_DIR / filename

    lines = [
        f"# Research Nota v2: {now.strftime('%d %B %Y, %H:%M UTC')}",
        "",
        "## Samenvatting",
        f"- Totaal resolved trades: {new_params.get('total_resolved_trades', 0)}",
        f"- Overall ROI: {new_params.get('overall_roi', 0)*100:.1f}%",
        f"- Geblokkeerde bronnen: {', '.join(new_params.get('blocked_sources', [])) or 'geen'}",
        f"- Strategy versie: {new_params.get('version', 1)}",
        "",
    ]

    if source_stats:
        lines.append("## Resultaten per Signaal Bron")
        lines.append("| Bron | Trades | Wins | Losses | Kosten | Winst | ROI | Win% |")
        lines.append("|------|--------|------|--------|--------|-------|-----|------|")
        for src, s in sorted(source_stats.items(), key=lambda x: -x[1]["trades"]):
            blocked = " GEBLOKKEERD" if src in new_params.get("blocked_sources", []) else ""
            lines.append(
                f"| {src}{blocked} | {s['trades']} | {s['wins']} | {s['losses']} | "
                f"${s['total_cost']:.2f} | ${s['total_profit']:.2f} | "
                f"{s['roi']*100:.1f}% | {s['win_rate']*100:.0f}% |"
            )
        lines.append("")

    if category_stats:
        lines.append("## Resultaten per Categorie")
        lines.append("| Categorie | Trades | Wins | ROI | Win% |")
        lines.append("|-----------|--------|------|-----|------|")
        for cat, s in sorted(category_stats.items(), key=lambda x: -x[1]["trades"]):
            lines.append(
                f"| {cat} | {s['trades']} | {s['wins']} | "
                f"{s['roi']*100:.1f}% | {s['win_rate']*100:.0f}% |"
            )
        lines.append("")

    if insights:
        lines.append("## Inzichten & Beslissingen")
        for i, insight in enumerate(insights, 1):
            lines.append(f"{i}. {insight}")
        lines.append("")

    lines.append("## Strategie Parameters")
    lines.append("```json")
    lines.append(json.dumps(new_params, indent=2))
    lines.append("```")

    filepath.write_text("\n".join(lines))
    log.info(f"Research note written: {filepath}")
    return filepath


def run_research_loop():
    """
    Main entry point. Called every 2 hours by the bot.
    Analyzes all resolved trades, updates source stats, blocks bad sources.
    """
    log.info("=== RESEARCH LOOP v2: analyzing resolved trades ===")

    pnl = load_json(PNL_FILE)
    trades = load_json(TRADES_FILE)

    if not pnl:
        log.info("No resolved trades yet — nothing to learn from.")
        return

    # 1. Analyze by source
    source_stats = analyze_by_source(pnl, trades)

    # 2. Analyze by category
    category_stats = analyze_by_category(pnl, trades)

    # 3. Load current params and update
    current_params = load_strategy_params()
    new_params, insights = update_strategy_params(source_stats, current_params)

    # 4. Save updated params
    save_json(STRATEGY_FILE, new_params)
    log.info(f"Strategy params updated to v{new_params.get('version', '?')}")

    # 5. Write research note
    write_research_note(source_stats, category_stats, insights, new_params)

    # 6. Log insights
    for insight in insights:
        log.info(f"LEARNED: {insight}")

    log.info("=== RESEARCH LOOP v2 complete ===")
    return new_params
