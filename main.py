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
