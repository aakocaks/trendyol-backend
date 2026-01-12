from fastapi import FastAPI
import requests
import os
import base64
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# ENV VARIABLES
API_KEY = os.getenv("TRENDYOL_API_KEY")
API_SECRET = os.getenv("TRENDYOL_API_SECRET")
SELLER_ID = os.getenv("TRENDYOL_SELLER_ID")


@app.get("/")
def health():
    return {
        "status": "ok",
        "env_loaded": all([API_KEY, API_SECRET, SELLER_ID])
    }


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


@app.get("/orders")
def orders():
    return get_orders()


@app.get("/profit")
def profit():
    orders = get_orders()
    results = []

    for order in orders:
        cargo_price = order.get("deliveryFee", 0)

        total_sales = 0
        total_commission = 0

        for line in order["lines"]:
            sales = line["lineGrossAmount"]
            commission_rate = line.get("commission", 0) / 100
            commission = sales * commission_rate

            total_sales += sales
            total_commission += commission

        net_profit = total_sales - total_commission - cargo_price

        results.append({
            "orderNumber": order["orderNumber"],
            "totalSales": round(total_sales, 2),
            "commission": round(total_commission, 2),
            "cargo": round(cargo_price, 2),
            "netProfit": round(net_profit, 2)
        })

    return results
