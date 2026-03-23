"""Explore active Polymarket markets to find trading opportunities."""
import requests
import json
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"


def get_active_markets(limit=50):
    """Fetch active markets from Polymarket Gamma API."""
    r = requests.get(f"{GAMMA_API}/markets", params={
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    })
    return r.json()


def analyze_market(market):
    """Extract key info from a market."""
    return {
        "question": market.get("question", ""),
        "slug": market.get("slug", ""),
        "volume24hr": float(market.get("volume24hr", 0)),
        "volume": float(market.get("volume", 0)),
        "liquidity": float(market.get("liquidity", 0)),
        "outcomePrices": market.get("outcomePrices", ""),
        "outcomes": market.get("outcomes", ""),
        "clobTokenIds": market.get("clobTokenIds", ""),
        "endDate": market.get("endDate", ""),
        "spread": market.get("spread", ""),
    }


def main():
    print(f"Fetching active markets at {datetime.now().isoformat()}...\n")
    markets = get_active_markets(100)

    if not markets:
        print("No markets returned!")
        return

    # Sort by 24h volume
    analyzed = []
    for m in markets:
        a = analyze_market(m)
        if a["volume24hr"] > 0:
            analyzed.append(a)

    analyzed.sort(key=lambda x: x["volume24hr"], reverse=True)

    print(f"Found {len(analyzed)} active markets with volume\n")
    print("=" * 80)
    print(f"{'#':<4} {'Volume 24h':>12} {'Liquidity':>12} {'Prices':<20} {'Question'}")
    print("=" * 80)

    for i, m in enumerate(analyzed[:30]):
        prices = m["outcomePrices"]
        if isinstance(prices, str) and prices:
            try:
                prices = json.loads(prices)
                prices = "/".join([f"{float(p):.2f}" for p in prices[:2]])
            except (json.JSONDecodeError, ValueError):
                prices = str(prices)[:18]

        question = m["question"][:50]
        print(f"{i+1:<4} ${m['volume24hr']:>10,.0f} ${m['liquidity']:>10,.0f} {prices:<20} {question}")

    # Show detailed info for top 5
    print("\n\n=== TOP 5 DETAILED ===\n")
    for i, m in enumerate(analyzed[:5]):
        print(f"--- #{i+1}: {m['question']} ---")
        print(f"  Slug: {m['slug']}")
        print(f"  24h Volume: ${m['volume24hr']:,.0f}")
        print(f"  Total Volume: ${m['volume']:,.0f}")
        print(f"  Liquidity: ${m['liquidity']:,.0f}")
        print(f"  Prices: {m['outcomePrices']}")
        print(f"  Outcomes: {m['outcomes']}")
        print(f"  CLOB Token IDs: {m['clobTokenIds']}")
        print(f"  End Date: {m['endDate']}")
        print()


if __name__ == "__main__":
    main()
