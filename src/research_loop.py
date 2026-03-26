"""
Research Loop v5 - Claude als mentor & coach met tools.

Claude krijgt de rol van top trading coach. Hij kan:
- Actuele crypto prijzen ophalen (Binance)
- Sportsbook odds checken (Odds API)
- Polymarket markten scannen (Gamma API)
- Vorige logboek entries lezen voor continuïteit

Elke 2 uur:
1. Claude krijgt alle data + tools
2. Claude analyseert, haalt extra info op waar nodig
3. Claude geeft advies en past parameters aan
4. Sessie wordt geschreven naar lokaal logboek
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("autobot")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STRATEGY_FILE = DATA_DIR / "strategy_params.json"
PNL_FILE = DATA_DIR / "pnl.json"
TRADES_FILE = DATA_DIR / "trades.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
LOGBOOK_FILE = BASE_DIR / "reports" / "logboek.md"


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return [] if str(path).endswith(".json") else {}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tools die Claude kan aanroepen
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_crypto_prices",
        "description": "Haal actuele crypto prijzen op van Binance. Geeft BTC, ETH en optioneel andere pairs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lijst van trading pairs, bijv. ['BTCUSDT', 'ETHUSDT']"
                }
            },
            "required": ["symbols"]
        }
    },
    {
        "name": "get_polymarket_markets",
        "description": "Zoek actieve Polymarket markten. Kan filteren op zoekterm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Zoekterm om markten te filteren, bijv. 'Bitcoin' of 'NBA'"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max aantal resultaten (default 20)"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_sportsbook_odds",
        "description": "Haal sportsbook odds op van the-odds-api.com voor een specifieke sport.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {
                    "type": "string",
                    "description": "Sport key, bijv. 'basketball_nba', 'icehockey_nhl', 'tennis_atp_miami_open'"
                }
            },
            "required": ["sport"]
        }
    },
    {
        "name": "get_market_detail",
        "description": "Haal gedetailleerde info op over een specifieke Polymarket markt via slug.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Market slug, bijv. 'will-bitcoin-reach-100k'"
                }
            },
            "required": ["slug"]
        }
    },
]


def execute_tool(name: str, input_data: dict) -> str:
    """Voer een tool uit en geef het resultaat als string terug."""
    try:
        if name == "get_crypto_prices":
            symbols = input_data.get("symbols", ["BTCUSDT", "ETHUSDT"])
            results = {}
            for sym in symbols[:10]:
                r = requests.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": sym}, timeout=10
                )
                if r.status_code == 200:
                    results[sym] = float(r.json()["price"])
            return json.dumps(results)

        elif name == "get_polymarket_markets":
            search = input_data.get("search", "")
            limit = input_data.get("limit", 20)
            params = {
                "active": "true", "closed": "false",
                "limit": min(limit, 50),
                "order": "volume24hr", "ascending": "false",
            }
            if search:
                params["tag"] = search
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params=params, timeout=15
            )
            if r.status_code == 200:
                markets = r.json()
                summary = []
                for m in markets[:limit]:
                    summary.append({
                        "question": m.get("question", "")[:100],
                        "slug": m.get("slug", ""),
                        "outcomePrices": m.get("outcomePrices", ""),
                        "volume24hr": round(float(m.get("volume24hr", 0)), 0),
                        "endDate": m.get("endDate", "")[:10],
                        "negRisk": m.get("negRisk", False),
                    })
                return json.dumps(summary, indent=2)
            return f"Error: {r.status_code}"

        elif name == "get_sportsbook_odds":
            sport = input_data.get("sport", "basketball_nba")
            api_key = os.getenv("ODDS_API_KEY", "")
            if not api_key:
                return "ODDS_API_KEY niet beschikbaar"
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                params={
                    "apiKey": api_key,
                    "regions": "us,eu",
                    "markets": "h2h,totals",
                    "oddsFormat": "decimal",
                },
                timeout=15
            )
            if r.status_code == 200:
                remaining = r.headers.get("x-requests-remaining", "?")
                events = r.json()
                summary = []
                for e in events[:15]:
                    summary.append({
                        "teams": f"{e.get('home_team','')} vs {e.get('away_team','')}",
                        "commence": e.get("commence_time", "")[:16],
                        "bookmakers": len(e.get("bookmakers", [])),
                    })
                return json.dumps({"remaining_credits": remaining, "events": summary}, indent=2)
            return f"Error: {r.status_code} - {r.text[:200]}"

        elif name == "get_market_detail":
            slug = input_data.get("slug", "")
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug}, timeout=15
            )
            if r.status_code == 200:
                markets = r.json()
                if markets:
                    m = markets[0]
                    return json.dumps({
                        "question": m.get("question", ""),
                        "outcomePrices": m.get("outcomePrices", ""),
                        "volume24hr": m.get("volume24hr", 0),
                        "liquidity": m.get("liquidityClob", 0),
                        "endDate": m.get("endDate", ""),
                        "description": m.get("description", "")[:500],
                    }, indent=2)
            return "Markt niet gevonden"

        return f"Onbekende tool: {name}"

    except Exception as e:
        return f"Tool error: {e}"


# ---------------------------------------------------------------------------
# Logboek
# ---------------------------------------------------------------------------

def read_recent_logbook(max_entries: int = 3) -> str:
    """Lees de laatste logboek entries voor continuïteit."""
    if not LOGBOOK_FILE.exists():
        return "Geen eerdere sessies."

    content = LOGBOOK_FILE.read_text()
    # Split op sessie headers
    sessions = content.split("\n---\n")
    recent = sessions[-max_entries:] if len(sessions) > max_entries else sessions
    return "\n---\n".join(recent)


def append_to_logbook(entry: str):
    """Voeg een nieuwe sessie toe aan het logboek."""
    LOGBOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    separator = "\n---\n" if LOGBOOK_FILE.exists() and LOGBOOK_FILE.stat().st_size > 0 else ""
    with open(LOGBOOK_FILE, "a") as f:
        f.write(f"{separator}{entry}")


# ---------------------------------------------------------------------------
# Bouw de data samen voor Claude
# ---------------------------------------------------------------------------

def build_trading_summary(pnl, trades, positions, current_params):
    """Bouw een compacte samenvatting van alle trading data."""

    # P&L per bron
    source_stats = {}
    for record in pnl:
        src = record.get("signal_source", "unknown")
        if src not in source_stats:
            source_stats[src] = {"w": 0, "l": 0, "profit": 0.0, "cost": 0.0}
        if record.get("profit", 0) > 0:
            source_stats[src]["w"] += 1
        else:
            source_stats[src]["l"] += 1
        source_stats[src]["profit"] += record.get("profit", 0)
        source_stats[src]["cost"] += record.get("cost", 0)

    source_summary = ""
    for src, s in sorted(source_stats.items(), key=lambda x: -x[1]["profit"]):
        n = s["w"] + s["l"]
        wr = s["w"] / n * 100 if n else 0
        roi = s["profit"] / s["cost"] * 100 if s["cost"] else 0
        source_summary += f"  {src}: {s['w']}W/{s['l']}L ({wr:.0f}% WR) profit=${s['profit']:.2f} ROI={roi:.1f}%\n"

    # Subcategorie stats
    subcat_stats = {}
    for record in pnl:
        market = record.get("market", "").lower()
        src = record.get("signal_source", "unknown")
        if "o/u" in market:
            sub = f"{src}:O/U"
        elif " vs " in market or " vs. " in market:
            sub = f"{src}:moneyline"
        elif "bitcoin" in market or "ethereum" in market:
            sub = f"{src}:crypto"
        else:
            sub = f"{src}:other"
        if sub not in subcat_stats:
            subcat_stats[sub] = {"w": 0, "l": 0, "profit": 0.0}
        if record.get("profit", 0) > 0:
            subcat_stats[sub]["w"] += 1
        else:
            subcat_stats[sub]["l"] += 1
        subcat_stats[sub]["profit"] += record.get("profit", 0)

    subcat_summary = ""
    for sub, s in sorted(subcat_stats.items(), key=lambda x: -x[1]["profit"]):
        n = s["w"] + s["l"]
        wr = s["w"] / n * 100 if n else 0
        subcat_summary += f"  {sub}: {s['w']}W/{s['l']}L ({wr:.0f}%) ${s['profit']:+.2f}\n"

    # Portfolio: haal echte waarden op via API
    open_pos = [p for p in positions if p.get("status") == "open"]
    total_cost = sum(p.get("cost", 0) for p in open_pos)
    try:
        # Data API voor echte positie waarde, CLOB voor cash
        funder = os.getenv("FUNDER_ADDRESS", "").lower()
        if funder:
            r = requests.get(f"https://data-api.polymarket.com/positions",
                             params={"user": funder}, timeout=15)
            r.raise_for_status()
            total_value = sum(float(p.get("currentValue", 0)) for p in r.json()
                              if float(p.get("size", 0)) >= 0.1)
        else:
            total_value = sum(p.get("current_value", 0) for p in open_pos)
        # Cash balance
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams
        clob = ClobClient("https://clob.polymarket.com", key=os.getenv("PRIVATE_KEY"),
                           chain_id=137, signature_type=2, funder=os.getenv("FUNDER_ADDRESS"))
        clob.set_api_creds(clob.derive_api_key())
        cash_balance = int(clob.get_balance_allowance(
            BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=2)
        ).get("balance", "0")) / 1e6
    except Exception as e:
        log.debug(f"Portfolio fetch failed: {e}")
        total_value = sum(p.get("current_value", 0) for p in open_pos)
        cash_balance = 0.0
    portfolio_total = total_value + cash_balance

    pos_summary = ""
    for p in sorted(open_pos, key=lambda x: -x.get("cost", 0))[:15]:
        pnl_val = p.get("unrealized_pnl", 0)
        pos_summary += f"  {p['market'][:55]} | {p.get('side','?')} | ${p.get('cost',0):.2f} | ${pnl_val:+.2f} | {p.get('signal_source','?')}\n"

    # Laatste trades
    recent = ""
    for t in trades[-15:]:
        recent += f"  {t.get('timestamp','')[:16]} {t.get('side','?')} {t.get('market','')[:45]} ${t.get('cost',0):.2f} [{t.get('signal_source','?')}] div={t.get('divergence',0):.2%}\n"

    # Correlatie check
    from collections import defaultdict
    event_losses = defaultdict(lambda: {"n": 0, "loss": 0.0})
    for record in pnl:
        if record.get("profit", 0) >= 0:
            continue
        name = record.get("market", "").lower()
        for sep in [":", " o/u"]:
            name = name.split(sep)[0]
        event_losses[name.strip()]["n"] += 1
        event_losses[name.strip()]["loss"] += record.get("profit", 0)
    corr = [f"  {e}: {s['n']} bets, ${s['loss']:.2f}" for e, s in event_losses.items() if s["n"] >= 2]

    return f"""## Portfolio (LIVE data van Polymarket API)
- TOTAAL: ${portfolio_total:.2f} (cash: ${cash_balance:.2f} + posities: ${total_value:.2f})
- Open posities: {len(open_pos)}, cost: ${total_cost:.2f}, waarde: ${total_value:.2f}, unrealized: ${total_value - total_cost:+.2f}
- Resolved trades: {len(pnl)}, realized P&L: ${sum(r.get('profit', 0) for r in pnl):.2f}

## Per bron
{source_summary}
## Per subcategorie
{subcat_summary}
## Open posities (top 15)
{pos_summary}
## Laatste 15 trades
{recent}
## Gecorreleerde verliezen
{chr(10).join(corr) if corr else "  Geen"}

## Huidige parameters
{json.dumps(current_params, indent=2)}"""


# ---------------------------------------------------------------------------
# Claude coach sessie
# ---------------------------------------------------------------------------

def run_coach_session(pnl, trades, positions, current_params):
    """Draai een volledige coaching sessie met Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY niet gezet — kan geen AI coaching draaien")
        return None

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    # Data samenvatting
    trading_summary = build_trading_summary(pnl, trades, positions, current_params)

    # Vorige sessies voor continuïteit
    prev_sessions = read_recent_logbook(3)

    system_prompt = """Je bent de mentor en top trading coach van een autonome Polymarket trading bot.

DOEL: Groei $180 startkapitaal naar $1.000 in 90 dagen (deadline: 21 juni 2026).

JE ROL:
- Analyseer de trading resultaten met een scherp oog
- Gebruik je tools om actuele marktdata op te halen als dat je analyse verbetert
- Geef concrete, actionable adviezen
- Pas de strategie parameters aan
- Wees eerlijk en direct — als iets niet werkt, zeg het

JE HEBT TOOLS:
- get_crypto_prices: actuele crypto prijzen (Binance)
- get_sportsbook_odds: sportsbook odds (the-odds-api.com)
- get_polymarket_markets: actieve Polymarket markten
- get_market_detail: details van een specifieke markt

GEBRUIK JE TOOLS als je:
- Wilt checken of open posities nog kans maken (haal actuele prijzen op)
- Wilt zien welke markten er nu beschikbaar zijn
- Odds wilt vergelijken met Polymarket prijzen

AAN HET EINDE van je analyse, geef ALTIJD een JSON blok met parameter updates:

```json
{
    "source_kelly": {"bron": 0.XX},
    "source_thresholds": {"bron": 0.XX},
    "source_max_fraction": {"bron": 0.XX},
    "blocked_sources": ["bron"],
    "blocked_subcategories": ["subcat"],
    "position_sizing": {"max_fraction": 0.XX, "kelly_multiplier": 0.XX},
    "max_theme_fraction": 0.XX,
    "stop_loss_pct": 0.XX
}
```

Schrijf je analyse in het Nederlands. Wees concreet, geen vage adviezen."""

    user_message = f"""## Vorige coaching sessies
{prev_sessions}

## Huidige trading data
{trading_summary}

Analyseer de situatie. Haal actuele marktdata op als dat relevant is. Geef je coaching advies en parameter updates."""

    messages = [{"role": "user", "content": user_message}]

    # Multi-turn loop met tool use
    full_response_text = ""
    max_turns = 10

    for turn in range(max_turns):
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Verwerk response content blocks
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        # Check voor tool calls
        tool_calls = [b for b in assistant_content if b.type == "tool_use"]
        text_blocks = [b for b in assistant_content if b.type == "text"]

        for tb in text_blocks:
            full_response_text += tb.text + "\n"

        if not tool_calls or response.stop_reason == "end_turn":
            break

        # Voer tools uit
        tool_results = []
        for tc in tool_calls:
            log.info(f"COACH TOOL: {tc.name}({json.dumps(tc.input)[:100]})")
            result = execute_tool(tc.name, tc.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result[:3000],  # cap tool output
            })

        messages.append({"role": "user", "content": tool_results})

    return full_response_text


def extract_params_from_response(text: str) -> dict | None:
    """Extract JSON parameters from Claude's response."""
    try:
        # Zoek JSON blok in de response
        if "```json" in text:
            json_str = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            # Probeer elk code block
            for block in text.split("```")[1::2]:
                block = block.strip()
                if block.startswith("json"):
                    block = block[4:].strip()
                if block.startswith("{"):
                    json_str = block
                    break
            else:
                return None
        else:
            # Zoek naar { ... } patroon
            start = text.rfind("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = text[start:end]
            else:
                return None

        return json.loads(json_str)
    except (json.JSONDecodeError, UnboundLocalError):
        log.warning("Kon geen JSON parameters uit Claude response extraheren")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_research_loop():
    """
    Elke 2 uur: stuur data naar Claude coach, krijg analyse + parameter updates terug.
    Schrijf sessie naar lokaal logboek.
    """
    log.info("=== AI COACH SESSIE v5 ===")

    pnl = load_json(PNL_FILE)
    trades = load_json(TRADES_FILE)
    positions = load_json(POSITIONS_FILE)

    if not pnl or len(pnl) < 3:
        log.info("Te weinig data voor coaching sessie.")
        return

    current_params = load_json(STRATEGY_FILE) or {}

    # Draai coaching sessie
    log.info(f"Start coaching sessie met {len(pnl)} resolved trades...")
    response_text = run_coach_session(pnl, trades, positions, current_params)

    if not response_text:
        log.error("Coaching sessie mislukt")
        return

    # Extract en pas parameters aan
    new_params_update = extract_params_from_response(response_text)
    if new_params_update:
        new_params = json.loads(json.dumps(current_params))
        for key in ["source_kelly", "source_thresholds", "source_max_fraction",
                     "blocked_sources", "blocked_subcategories", "position_sizing"]:
            if key in new_params_update:
                new_params[key] = new_params_update[key]
        if "max_theme_fraction" in new_params_update:
            new_params["max_theme_fraction"] = new_params_update["max_theme_fraction"]
        if "stop_loss_pct" in new_params_update:
            new_params["stop_loss_pct"] = new_params_update["stop_loss_pct"]

        new_params["version"] = current_params.get("version", 0) + 1
        new_params["updated_at"] = datetime.now(timezone.utc).isoformat()
        new_params["last_coach_session"] = datetime.now(timezone.utc).isoformat()
        save_json(STRATEGY_FILE, new_params)
        log.info(f"Parameters bijgewerkt naar v{new_params['version']} (coach)")
    else:
        log.warning("Geen parameter updates geëxtraheerd uit coaching sessie")

    # Schrijf naar logboek
    now = datetime.now(timezone.utc)
    logbook_entry = f"""## Sessie {now.strftime('%Y-%m-%d %H:%M UTC')}

{response_text.strip()}
"""
    append_to_logbook(logbook_entry)
    log.info(f"Logboek bijgewerkt: {LOGBOOK_FILE}")

    # Log samenvatting
    for line in response_text.strip().split("\n"):
        if line.strip():
            log.info(f"COACH: {line.strip()[:120]}")
            break  # Log alleen eerste niet-lege regel

    log.info("=== AI COACH SESSIE v5 compleet ===")
    return new_params_update
