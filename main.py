from fastapi import FastAPI
import os
import requests
import base64
from cargo import calculate_cargo_cost

app = FastAPI()

# ENV
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

    return
