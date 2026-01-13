from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import os
import requests
import base64
from datetime import datetime
from openpyxl import Workbook
import io

app = FastAPI()

# =========================
# SABİTLER
# =========================
FATURA_ORANI = 0.10  # %10 fatura

# =========================
# KONTROL
# =========================
@app.get("/")
def root():
    return {"ok": True}

@app.get("/health")
def health():
    return {"status": "running"}

@app.get("/env")
def env_check():
    return {
        "API_KEY_SET": bool(os.getenv("TRENDYOL_API_KEY")),
        "API_SECRET_SET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "SELLER_ID_SET": bool(os.getenv("TRENDYOL_SELLER_ID")),
    }

# =========================
# TRENDYOL ORDERS (TEK YERDEN)
# =========================
def fetch_orders():
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    auth = f"{api_key}:{api_secret}"
    encoded = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"
    headers = {
        "Authorization": f"Basic {encoded}",
        "User-Agent": f"{seller_id} - Trendyol API"
    }

    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json().get("content", [])

@app.get("/orders")
def orders():
    return fetch_orders()

# =========================
# SUMMARY (JSON)
# =========================
@app.get("/summary")
def summary(start: str, end: str):
    start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)

    orders = fetch_orders()

    toplam_siparis = 0
    toplam_ciro = 0.0
    toplam_komisyon = 0.0
    toplam_kargo = 0.0

    for order in orders:
        if start_ts <= order.get("orderDate", 0) <= end_ts:
            toplam_siparis += 1
            for line in order.get("lines", []):
                toplam_ciro += line.get("price", 0)
                toplam_komisyon += line.get("commission", 0)
                toplam_kargo += line.get("cargoPrice", 0)

    fatura = toplam_ciro * FATURA_ORANI
    net = toplam_ciro - toplam_komisyon - toplam_kargo - fatura

    return {
        "toplam_siparis": toplam_siparis,
        "toplam_ciro": round(toplam_ciro, 2),
        "toplam_komisyon": round(toplam_komisyon, 2),
        "toplam_kargo": round(toplam_kargo, 2),
        "fatura_%10": round(fatura, 2),
        "gercek_net_kar": round(net, 2)
    }

# =========================
# SUMMARY EXCEL
# =========================
@app.get("/summary/excel")
def summary_excel(start: str, end: str):
    start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)

    orders = fetch_orders()

    wb = Workbook()
    ws = wb.active
    ws.title = "Kar-Zarar"

    ws.append([
        "Tarih",
        "Sipariş No",
        "Ciro",
        "Komisyon",
        "Kargo",
        "Fatura %10",
        "Net Kar"
    ])

    for order in orders:
        if start_ts <= order.get("orderDate", 0) <= end_ts:
            tarih = datetime.fromtimestamp(order["orderDate"] / 1000).strftime("%Y-%m-%d")
            for line in order.get("lines", []):
                ciro = line.get("price", 0)
                komisyon = line.get("commission", 0)
                kargo = line.get("cargoPrice", 0)
                fatura = ciro * FATURA_ORANI
                net = ciro - komisyon - kargo - fatura

                ws.append([
                    tarih,
                    order.get("orderNumber"),
                    round(ciro, 2),
                    round(komisyon, 2),
                    round(kargo, 2),
                    round(fatura, 2),
                    round(net, 2)
                ])

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)

    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=kar_zarar.xlsx"}
    )
from datetime import date

@app.get("/summary/today")
def today_summary():
    today = date.today().strftime("%Y-%m-%d")
    return summary(start=today, end=today)
@app.get("/summary/month")
def month_summary():
    today = date.today()
    start = today.replace(day=1).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    return summary(start=start, end=end)
