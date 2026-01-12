from fastapi import FastAPI
import requests
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# =====================
# ENV BİLGİLERİ
# =====================
SELLER_ID = os.getenv("SELLER_ID")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# =====================
# TEST ENDPOINT
# =====================
@app.get("/")
def root():
    return {
        "status": "ok",
        "env_loaded": bool(SELLER_ID and API_KEY and API_SECRET)
    }

# =====================
# TRENDYOL ORDERS
# =====================
@app.get("/orders")
def get_orders():
    auth = f"{API_KEY}:{API_SECRET}"
    encoded_auth = auth.encode("ascii")
    import base64
    encoded_auth = base64.b64encode(encoded_auth).decode("ascii")

    url = f"https://api.trendyol.com/sapigw/suppliers/{SELLER_ID}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{SELLER_ID} - Trendyol API"
    }

    response = requests.get(url, headers=headers)
    return response.json()

# =====================
# KAR / ZARAR HESABI
# =====================
@app.get("/profit")
def calculate_profit():
    data = get_orders()
    orders = data.get("content", [])

    KARGO_UCRETI = 60  # şimdilik sabit
    results = []

    for order in orders:
        order_total = 0

        for line in order.get("lines", []):
            order_total += line.get("lineGrossAmount", 0)

        net = order_total - KARGO_UCRETI

        results.append({
            "orderNumber": order.get("orderNumber"),
            "sales": order_total,
            "cargo": KARGO_UCRETI,
            "profit": net
        })

    return results
