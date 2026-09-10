def quote(prices, discount=0):
    """Return discounted average price; discount is a fraction."""
    return sum(prices) / len(prices) * (1 - discount)
