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
