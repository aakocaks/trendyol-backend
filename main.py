from cargo import calculate_cargo_cost
from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/")
def root():
    return {
        "status": "ok",
        "env_loaded": bool(os.getenv("TEST_ENV"))
    }
import base64
import requests
import os
from fastapi import FastAPI

app = FastAPI()

SELLER_ID = os.getenv("TRENDYOL_SELLER_ID")
API_KEY = os.getenv("TRENDYOL_API_KEY")
API_SECRET = os.getenv("TRENDYOL_API_SECRET")

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

    return response.json()
@app.get("/profit")
def calculate_profit():
    orders = get_orders_from_trendyol()  # senin mevcut fonksiyonun

    KARGO_UCRETI = 60  # sabit, sonra dinamik yaparız
    results = []

    for order in orders:
        for line in order["lines"]:
            sales = line["lineGrossAmount"]
            commission = order.get("commission", 0)

            vat_rate = line.get("vatRate", 0) / 100
            vat = (sales - commission) * vat_rate

            net_profit = sales - commission - vat - KARGO_UCRETI

            results.append({
                "orderNumber": order["orderNumber"],
                "product": line["productName"],
                "sales": sales,
                "commission": commission,
                "vat": vat,
                "cargo": KARGO_UCRETI,
                "netProfit": round(net_profit, 2)
            })

    return {
        "totalOrders": len(results),
        "results": results
    }
