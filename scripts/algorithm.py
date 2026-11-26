import json, time, os
import requests

MARKET_URL = "https://raw.githubusercontent.com/exofri/ensim-trade-center/refs/heads/main/docs/data/market.json"
HOLDINGS_URL = "http://raw.githubusercontent.com/exofri/ensim-trade-center/refs/heads/main/traders/Oumayma Brahmi/holdings.json"

MIN_MONEY_RESERVE = 50 # never let a buy signal spend below this much money

# ---------------------------------------------------------------------------
# My strategy settings (chosen from the observed price history)
#   A: oscillates around a stable mean, wave of ~48 ticks -> mean-reversion
#   B: rising trend                                        -> keep a core position, buy the dips
#   C: falling trend, wave of ~24 ticks                    -> stay out
# ---------------------------------------------------------------------------
WINDOW = {"A": 48, "B": 48, "C": 24}   # one full wave, so the average cancels the wave out
MAX_SHARE = {"A": 0.6, "B": 0.4, "C": 0.0}  # max share of my total wealth in each product (no all-in)
K = 1.0                                # band width: "unusual" = more than 1 std from the average


def fetch_json(url):
    resp = requests.get(url, proxies={"http": None, "https": None}, timeout=(10, 20))
    resp.raise_for_status()
    return resp.json()


def mean(xs):
    return sum(xs) / len(xs)


def std(xs):
    m = mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def units_held(holdings, product):
    # holdings.json structure: adapt this if your file stores products differently
    if product in holdings:
        return float(holdings[product])
    for key in ("products", "holdings", "units"):
        if isinstance(holdings.get(key), dict) and product in holdings[key]:
            return float(holdings[key][product])
    return 0.0


def target_share(prices, product, current_share):
    """Share of my total wealth I WANT in this product (0 = none). Returns (share, reason)."""
    w = WINDOW[product]
    if len(prices) < 2 * w:
        return 0.0, "not enough history yet"

    recent = prices[-w:]
    avg = mean(recent)
    sd = std(recent)
    if sd == 0:
        return current_share, "price flat, keep position"

    z = (prices[-1] - avg) / sd                    # Bollinger position: how unusual is the price?
    trend = (avg - mean(prices[-2 * w:-w])) / avg  # this wave's average vs the previous one
    cap = MAX_SHARE[product]

    if product == "A":                             # no trend -> pure mean-reversion
        if z < -K:
            return cap, f"price low (z={z:.2f}) -> buy, expect return to average"
        if z > K:
            return 0.0, f"price high (z={z:.2f}) -> sell, expect return to average"
        return current_share, f"price normal (z={z:.2f}) -> keep position"

    if trend <= 0:                                 # falling product -> stay out
        return 0.0, f"downtrend ({trend:+.1%} per wave) -> hold nothing"

    # rising product: core position (half the cap), more on dips, trim on spikes
    if z < -K:
        return cap, f"uptrend + dip (z={z:.2f}) -> buy more"
    if z > K:
        return cap * 0.5, f"uptrend + spike (z={z:.2f}) -> trim to core"
    return max(current_share, cap * 0.5), f"uptrend ({trend:+.1%}) -> keep core position"


def compute_signal(prices, product, holdings, total_value):
    """
    Returns a signal in [-1, 1]: the order needed to move from what I hold now
    to the position I want (target), in units of 100.
    """
    if not prices or total_value <= 0:
        return 0.0, "no data"
    price = prices[-1]
    held = units_held(holdings, product)
    current_share = held * price / total_value
    share, reason = target_share(prices, product, current_share)
    target_units = share * total_value / price
    signal = (target_units - held) / 100
    signal = max(-1.0, min(1.0, signal))
    if abs(signal) < 0.01:                         # ignore tiny adjustments
        signal = 0.0
    if signal < 0:                                 # infeasible: can't sell more than I hold
        signal = max(signal, -held / 100)
    return signal, reason


def apply_budget_guard(raw_signal, money, price, reserve=MIN_MONEY_RESERVE):
    # A buy signal (positive) that would spend into the reserve gets scaled down,
    # not silently zeroed out -- you still act on the signal, just smaller. A sell
    # signal is never touched here: selling only ever adds money, never risks it.
    if raw_signal <= 0:
        return raw_signal
    if money <= reserve:
        return 0.0
    max_affordable_units = (money - reserve) / price
    desired_units = 100 * raw_signal
    if desired_units <= max_affordable_units:
        return raw_signal
    return round(max_affordable_units / 100, 3)


def main():
    market = fetch_json(MARKET_URL)
    holdings = fetch_json(HOLDINGS_URL)
    products = market["products"]
    history = market["history"]

    last = history[-1] if history else {}
    total_value = holdings["money"] + sum(units_held(holdings, p) * last.get(p, 0) for p in products)

    decision, reasons = {}, {}
    money_left = holdings["money"]   # shared budget: two buys in the same tick can't both spend it all
    for p in products:
        prices = [row[p] for row in history]
        raw, reasons[p] = compute_signal(prices, p, holdings, total_value)
        current_price = prices[-1] if prices else 0
        decision[p] = round(apply_budget_guard(raw, money_left, current_price), 3)
        if decision[p] > 0:
            money_left -= 100 * decision[p] * current_price

    decision["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open("decision.json", "w") as f:
        json.dump(decision, f, indent=2)

    # decision log for the dashboard / report (one line per product per run)
    new_file = not os.path.exists("decision_log.csv")
    with open("decision_log.csv", "a") as f:
        if new_file:
            f.write("updated_at,product,price,signal,reason\n")
        for p in products:
            price = history[-1][p] if history else 0
            f.write(f'{decision["updated_at"]},{p},{price},{decision[p]},"{reasons[p]}"\n')

    print(f"Wrote decision.json: {decision}")
    for p in products:
        print(f"  {p}: {reasons[p]}")


if __name__ == "__main__":
    main()
