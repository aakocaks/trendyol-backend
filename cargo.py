CARGO_PRICES = {
    "Sürat Kargo": 65,
    "Yurtiçi Kargo": 70,
    "MNG Kargo": 75,
    "Aras Kargo": 72
}

def calculate_cargo_cost(order):
    return CARGO_PRICES.get(order.get("cargoProviderName"), 70)

def calculate_cargo(price: float) -> float:
    if price >= 300:
        return 0.0
    return 29.90
