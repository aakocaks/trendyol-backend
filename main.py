from fastapi import FastAPI
import os

app = FastAPI()

# Ana kontrol
@app.get("/")
def root():
    return {"ok": True}

# Health check (Render sever bunu)
@app.get("/health")
def health():
    return {
        "service": "trendyol-backend",
        "status": "running"
    }

# Ortam değişkenleri kontrolü
@app.get("/env")
def env_check():
    return {
        "API_KEY_SET": bool(os.getenv("TRENDYOL_API_KEY")),
        "API_SECRET_SET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "SELLER_ID_SET": bool(os.getenv("TRENDYOL_SELLER_ID")),
    }
import requests
import base64

@app.get("/orders")
def get_orders():
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    auth = f"{api_key}:{api_secret}"
    encoded_auth = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{seller_id} - Trendyol API"
    }

    r = requests.get(url, headers=headers)
    r.raise_for_status()

    return r.json()
@app.get("/summary")
def summary():
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    import base64, requests

    auth = f"{api_key}:{api_secret}"
    encoded_auth = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{seller_id} - Trendyol API"
    }

    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()

    orders = data.get("content", [])

    toplam_siparis = len(orders)
    toplam_ciro = 0
    toplam_komisyon = 0

    for order in orders:
        for line in order.get("lines", []):
            toplam_ciro += line.get("price", 0)
            toplam_komisyon += line.get("commission", 0)

    return {
        "toplam_siparis": toplam_siparis,
        "toplam_ciro": round(toplam_ciro, 2),
        "toplam_komisyon": round(toplam_komisyon, 2),
        "net_kazanc": round(toplam_ciro - toplam_komisyon, 2)
    }
