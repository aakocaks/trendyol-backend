from fastapi import FastAPI
from fastapi.responses import FileResponse
import os
import requests
import base64
from datetime import datetime, date
import pandas as pd
import tempfile

app = FastAPI()

FATURA_ORANI = 0.10  # %10 fatura

# --------------------
# BASIC ENDPOINTS
# --------------------

@app.get("/")
def root():
    return {"status": "ok", "env_loaded": True}

@app.get("/health")
def health():
    return {"service": "trendyol-backend", "status": "running"}

@app.get("/env")
def env_check():
    return {
        "API_KEY_SET": bool(os.getenv("TRENDYOL_API_KEY")),
        "API_SECRET_SET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "SELLER_ID_SET": bool(os.getenv("TRENDYOL_SELLER_ID")),
    }

# --------------------
# TRENDYOL ORDERS
# --------------------

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

    return r.json().get("content", [])

# --------------------
# EXCEL GENERATOR
# --------------------

def generate_excel(orders, start_date, end_date):
    rows = []

    for order in orders:
        order_ts = order.get("orderDate")
        if not order_ts:
            continue

        order_date = datetime.fromtimestamp(order_ts / 1000).date()
        if not (start_date <= order_date <= end_date):
            continue

        for line in order.get("lines", []):
            ciro = line.get("price", 0)
            komisyon = line.get("commission", 0)
            kargo = line.get("cargoPrice", 0)
            kdv = ciro * FATURA_ORANI
            net_kar = ciro - komisyon - kargo - kdv

            rows.append({
                "Sipariş Tarihi": order_date.isoformat(),
                "Ciro": round(ciro, 2),
                "Komisyon": round(komisyon, 2),
                "Kargo": round(kargo, 2),
                "KDV %10": round(kdv, 2),
                "Net Kar": round(net_kar, 2)
            })

    df = pd.DataFrame(rows)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    df.to_excel(tmp.name, index=False)

    return tmp.name

# --------------------
# TODAY REPORT
# --------------------

@app.get("/report/today")
def report_today():
    orders = get_orders()
    today = date.today()

    file_path = generate_excel(orders, today, today)

    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"trendyol_rapor_{today}.xlsx"
    )

# --------------------
# DATE RANGE REPORT
# --------------------

@app.get("/report")
def report_range(start: str, end: str):
    orders = get_orders()

    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()

    file_path = generate_excel(orders, start_date, end_date)

    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"trendyol_rapor_{start}_{end}.xlsx"
    )
