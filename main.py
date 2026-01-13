from fastapi import FastAPI
import os
import requests
import base64
from datetime import datetime

app = FastAPI()

# Ana kontrol
@app.get("/")
def root():
    return {"ok": True}

# Health check
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

# Siparişleri çek
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

# Tarihli özet (kâr / zarar)
@app.get("/summary")
def summary(start: str, end: str):
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)

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

    toplam_siparis = 0
    toplam_ciro = 0
    toplam_komisyon = 0
    toplam_kargo = 0

    for order in orders:
        order_date = order.get("orderDate", 0)
        if start_ts <= order_date <= end_ts:
            toplam_siparis += 1
            for line in order.get("lines", []):
                toplam_ciro += line.get("price", 0)
                toplam_komisyon += line.get("commission", 0)
                toplam_kargo += line.get("cargoPrice", 0)

    kdv = toplam_ciro * 0.10
    net_kar = toplam_ciro - toplam_komisyon - toplam_kargo - kdv

    return {
        "baslangic": start,
        "bitis": end,
        "toplam_siparis": toplam_siparis,
        "toplam_ciro": round(toplam_ciro, 2),
        "toplam_komisyon": round(toplam_komisyon, 2),
        "toplam_kargo": round(toplam_kargo, 2),
        "kesilen_kdv_%10": round(kdv, 2),
        "gercek_net_kar": round(net_kar, 2)
    }
from openpyxl import Workbook
from fastapi.responses import StreamingResponse
import io

@app.get("/summary/excel")
def summary_excel(start: str, end: str):
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)

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

    wb = Workbook()
    ws = wb.active
    ws.title = "Kar-Zarar"

    # Başlıklar
    ws.append([
        "Sipariş Tarihi",
        "Sipariş No",
        "Ciro",
        "Komisyon",
        "Kargo",
        "KDV %10",
        "Net Kar"
    ])

    for order in orders:
        order_date = order.get("orderDate", 0)
        if start_ts <= order_date <= end_ts:
            for line in order.get("lines", []):
                ciro = line.get("price", 0)
                komisyon = line.get("commission", 0)
                kargo = line.get("cargoPrice", 0)
                kdv = ciro * 0.10
                net = ciro - komisyon - kargo - kdv

                ws.append([
                    datetime.fromtimestamp(order_date / 1000).strftime("%Y-%m-%d"),
                    order.get("orderNumber"),
                    round(ciro, 2),
                    round(komisyon, 2),
                    round(kargo, 2),
                    round(kdv, 2),
                    round(net, 2)
                ])

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)

    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=kar_zarar_raporu.xlsx"
        }
    )
