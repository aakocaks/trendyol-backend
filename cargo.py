CARGO_PRICES = {
    "Sürat Kargo": 65,
    "Yurtiçi Kargo": 70,
    "MNG Kargo": 75,
    "Aras Kargo": 72
}

def calculate_cargo_cost(order):
    return CARGO_PRICES.get(order.get("cargoProviderName"), 70)
