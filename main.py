from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True}

@app.get("/health")
def health():
    return {
        "service": "trendyol-backend",
        "status": "running"
    }

@app.get("/env")
def env_check():
    return {
        "API_KEY_SET": bool(os.getenv("TRENDYOL_API_KEY")),
        "API_SECRET_SET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "SELLER_ID_SET": bool(os.getenv("TRENDYOL_SELLER_ID")),
        "MAIL_USER_SET": bool(os.getenv("MAIL_USER")),
        "MAIL_PASS_SET": bool(os.getenv("MAIL_PASS")),
        "MAIL_TO_SET": bool(os.getenv("MAIL_TO")),
    }
import requests
import base64

@app.get("/orders")
def get_orders():
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    # Güvenlik kontrolü
    if not api_key or not api_secret or not seller_id:
        return {"error": "Trendyol env bilgileri eksik"}

    auth = f"{api_key}:{api_secret}"
    encoded_auth = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{seller_id} - Trendyol API"
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return response.json()
