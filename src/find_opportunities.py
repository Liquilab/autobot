"""Find trading opportunities on Polymarket by analyzing spreads and mispricing."""
import requests
import json
from datetime import datetime, timezone

GAMMA_API = "https://gamma-api.polymarket.com"


def get_markets(limit=200):
    """Fetch active markets sorted by volume."""
    r = requests.get(f"{GAMMA_API}/markets", params={
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    })
    return r.json()


def analyze_opportunities(markets):
    """Find markets with good risk/reward opportunities."""
    opportunities = []

    for m in markets:
        try:
            prices_raw = m.get("outcomePrices", "")
            if not prices_raw:
                continue
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            if len(prices) < 2:
                continue

            yes_price = float(prices[0])
            no_price = float(prices[1])
            volume_24h = float(m.get("volume24hr", 0))
            liquidity = float(m.get("liquidity", 0))
            question = m.get("question", "")
            end_date = m.get("endDate", "")

            # Skip markets with no liquidity or at extreme prices
            if liquidity < 10000 or volume_24h < 50000:
                continue

            # Calculate implied probabilities and spreads
            spread = abs(1.0 - yes_price - no_price)

            # Look for markets where we can get good odds
            # Strategy: find events trading at extreme prices with high conviction
            opp = {
                "question": question,
                "slug": m.get("slug", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "volume_24h": volume_24h,
                "liquidity": liquidity,
                "spread": spread,
                "end_date": end_date,
                "clob_token_ids": m.get("clobTokenIds", ""),
                "neg_risk": m.get("negRisk", False),
            }

            # Categorize opportunity type
            if yes_price < 0.15 and volume_24h > 100000:
                opp["type"] = "LONGSHOT_YES"
                opp["potential_return"] = (1.0 / yes_price - 1) * 100
            elif no_price < 0.15 and volume_24h > 100000:
                opp["type"] = "LONGSHOT_NO"
                opp["potential_return"] = (1.0 / no_price - 1) * 100
            elif 0.35 < yes_price < 0.65:
                opp["type"] = "COIN_FLIP"
                opp["potential_return"] = max(
                    (1.0 / yes_price - 1) * 100,
                    (1.0 / no_price - 1) * 100
                )
            else:
                opp["type"] = "STANDARD"
                opp["potential_return"] = max(
                    (1.0 / yes_price - 1) * 100,
                    (1.0 / no_price - 1) * 100
                )

            opportunities.append(opp)

        except (json.JSONDecodeError, ValueError, IndexError, TypeError):
            continue

    return opportunities


def print_opportunities(opportunities):
    """Print formatted opportunity list."""
    # Group by type
    for opp_type in ["COIN_FLIP", "STANDARD", "LONGSHOT_YES", "LONGSHOT_NO"]:
        typed = [o for o in opportunities if o["type"] == opp_type]
        if not typed:
            continue

        typed.sort(key=lambda x: x["volume_24h"], reverse=True)
        print(f"\n{'='*80}")
        print(f"  {opp_type} MARKETS ({len(typed)})")
        print(f"{'='*80}")

        for o in typed[:10]:
            q = o["question"][:60]
            print(f"\n  {q}")
            print(f"  YES: ${o['yes_price']:.3f}  NO: ${o['no_price']:.3f}  "
                  f"Vol24h: ${o['volume_24h']:,.0f}  Liq: ${o['liquidity']:,.0f}")
            print(f"  Potential return: {o['potential_return']:.0f}%  "
                  f"End: {o['end_date'][:10] if o['end_date'] else 'N/A'}")
            print(f"  Slug: {o['slug']}")


def main():
    print(f"Scanning Polymarket for opportunities at {datetime.now().isoformat()}")
    markets = get_markets(200)
    opportunities = analyze_opportunities(markets)

    print(f"\nFound {len(opportunities)} opportunities across {len(markets)} markets")
    print_opportunities(opportunities)

    # Summary stats
    coin_flips = [o for o in opportunities if o["type"] == "COIN_FLIP"]
    print(f"\n\n=== SUMMARY ===")
    print(f"Total opportunities: {len(opportunities)}")
    print(f"Coin-flip markets (best for directional bets): {len(coin_flips)}")
    print(f"Longshot YES markets: {len([o for o in opportunities if o['type'] == 'LONGSHOT_YES'])}")
    print(f"Longshot NO markets: {len([o for o in opportunities if o['type'] == 'LONGSHOT_NO'])}")

    return opportunities


if __name__ == "__main__":
    main()
