"""
Trading Strategy for Polymarket Bot
Goal: $100 -> $1,000 in 90 days (10x return)

Strategy: Multi-approach portfolio trading

1. SPORTS MARKETS (40% of capital)
   - High volume, daily resolution
   - Focus on spreads and totals where we can find edge
   - Kelly criterion for position sizing
   - Target: 55-60% win rate at ~even odds = steady compounding

2. EVENT MARKETS (40% of capital)
   - Political events, crypto prices, geopolitical
   - Look for mispriced probabilities
   - Hold positions for days/weeks
   - Higher conviction, larger individual bets

3. MARKET MAKING (20% of capital)
   - Provide liquidity on high-volume markets
   - Earn the spread
   - Lower risk, steady small returns
   - Good for capital preservation

Position Sizing (Kelly Criterion):
   f* = (bp - q) / b
   where: b = odds, p = probability of winning, q = 1-p

Risk Management:
   - Max 15% of bankroll per single bet
   - Max 30% of bankroll in correlated positions
   - Stop trading if bankroll drops below $50
   - Reduce position sizes if on losing streak

To hit 10x in 90 days, we need ~2.6% daily compounding.
That's achievable with consistent edge and proper sizing.
"""

import math


def kelly_fraction(prob_win, odds):
    """Calculate Kelly fraction for optimal bet sizing.

    Args:
        prob_win: estimated probability of winning (0-1)
        odds: decimal odds (e.g., 2.0 means you get $2 for $1 bet)

    Returns:
        Fraction of bankroll to bet (0-1)
    """
    b = odds - 1  # net odds
    q = 1 - prob_win
    f = (b * prob_win - q) / b
    return max(0, f)


def half_kelly(prob_win, odds):
    """Half-Kelly for more conservative sizing."""
    return kelly_fraction(prob_win, odds) / 2


def position_size(bankroll, prob_win, price, max_fraction=0.15):
    """Calculate position size in dollars.

    Args:
        bankroll: total available capital
        prob_win: estimated probability of outcome
        price: current market price (0-1)
        max_fraction: maximum fraction of bankroll

    Returns:
        Dollar amount to bet
    """
    odds = 1.0 / price  # decimal odds from price
    kelly = half_kelly(prob_win, odds)
    fraction = min(kelly, max_fraction)
    return bankroll * fraction


def required_daily_return(target_multiple, days):
    """Calculate required daily return for target."""
    return (target_multiple ** (1 / days)) - 1


if __name__ == "__main__":
    # Calculate targets
    daily_r = required_daily_return(10, 90)
    print(f"Required daily return for 10x in 90 days: {daily_r*100:.2f}%")
    print(f"Required daily return for 10x in 60 days: {required_daily_return(10, 60)*100:.2f}%")

    # Example position sizing
    bankroll = 100
    print(f"\nExample position sizes (bankroll=${bankroll}):")

    scenarios = [
        ("60% win, price 0.50", 0.60, 0.50),
        ("70% win, price 0.50", 0.70, 0.50),
        ("55% win, price 0.40", 0.55, 0.40),
        ("80% win, price 0.70", 0.80, 0.70),
        ("90% win, price 0.85", 0.90, 0.85),
    ]

    for label, prob, price in scenarios:
        size = position_size(bankroll, prob, price)
        kelly = half_kelly(prob, 1/price)
        print(f"  {label}: bet ${size:.2f} (half-kelly: {kelly*100:.1f}%)")
