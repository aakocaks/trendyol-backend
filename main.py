from fastapi import FastAPI
import os
import requests
import base64

app = FastAPI()

# ENV
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
def orders():
    auth = f"{API_KEY}:{API_SECRET}"
    encoded_auth = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{SELLER_ID}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{SELLER_ID} - Trendyol API"
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return response.json()
