import os
import base64
import requests
from fastapi import FastAPI

app = FastAPI()

API_KEY = os.getenv("TRENDYOL_API_KEY")
API_SECRET = os.getenv("TRENDYOL_API_SECRET")
SELLER_ID = os.getenv("TRENDYOL_SELLER_ID")

@app.get("/")
def root():
    return {
        "status": "ok",
        "env_loaded": bool(API_KEY and API_SECRET and SELLER_ID)
    }

@app.get("/orders")
def get_orders():
    auth = f"{API_KEY}:{API_SECRET}"
    encoded_auth = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{SELLER_ID}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{SELLER_ID} - Trendyol API"
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    return data.get("content", [])

@app.get("/profit")
def profit():
    auth = f"{API_KEY}:{API_SECRET}"
    encoded_auth = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{SELLER_ID}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{SELLER_ID} - Trendyol API"
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()
    orders = response.json().get("content", [])

    results = []

    for order in orders:
        total_sales = 0
        for line in order.get("lines", []):
            total_sales += line.get("lineGrossAmount", 0)

        results.append({
            "orderNumber": order.get("orderNumber"),
            "totalSales": total_sales,
            "cargoTrackingNumber": order.get("cargoTrackingNumber"),
            "cargoProvider": order.get("cargoProviderName")
        })

    return results
