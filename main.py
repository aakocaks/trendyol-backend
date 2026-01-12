from fastapi import FastAPI
import requests
import os
import base64
from cargo import calculate_cargo_cost

app = FastAPI()

# ENV
API_KEY = os.getenv("TRENDYOL_API_KEY")
API_SECRET = os.getenv("TRENDYOL_API_SECRET")
SELLER_ID = os.getenv("TRENDYOL_SELLER_ID")

def get_auth_headers():
    auth = f"{API_KEY}:{API_SECRET}"
    encoded_auth = base64.b64encode(auth.encode()).decode()
    return {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{SELLER_ID} - Trendyol API"
    }

@app.get("/")
def health():
    return {
        "status": "ok",
        "env_loaded": all([API_KEY, API_SECRET, SELLER_ID])
    }

@app.get("/orders")
def get_orders():
    url = f"https://api.trendyol.com/sapigw/suppliers/{SELLER_ID}/orders"
    response = requests.get(url, headers=get_auth_headers())
    response.raise_for_status()
    return response.json().get("content", [])

@app.get("/profit")
def profit():
    url = f"https://api.trendyol.com/sapigw/suppliers/{SELLER_ID}/orders"
    response = requests.get(url, headers=get_auth_headers())
    response.raise_for_status()
    orders = response.json().get("content", [])

    items = []

    for order in orders:
        cargo_cost = calculate_cargo_cost(order)

        for line in order.get("lines", []):
            sale_price = line.get("price", 0)
            product = line.get("productName", "Ürün")
            order_number = order.get("orderNumber")

            items.append({
                "orderNumber": order_number,
                "product": product,
                "salePrice": sale_price,
                "cargo": cargo_cost
            })

    return {
        "count": len(items),
        "items": items
    }
