# -*- coding: utf-8 -*-
"""
app.py — واجهة REST API لنظام GSCSI (القضارف)
تحصينات: CORS مقيد + Rate Limit + Pydantic + كاش Redis/file + معالجة أخطاء آمنة
"""
import os
import time
import uuid
import logging
import traceback
import threading
import datetime
import shelve
import pathlib
from typing import Optional

from flask import Flask, jsonify, request, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pydantic import BaseModel, Field, field_validator, ValidationError

import backend.gscsi_engine as gscsi_engine
import backend.config as CFG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("gscsi-api")

app = Flask(__name__)
# CORS مقيد — يدعم github.io الفرعية عبر regex + قائمة ثابتة
import re as _re
_CORS_REGEX = _re.compile(CFG.CORS_ALLOWED_ORIGIN_REGEX)

def _is_origin_allowed(origin: str) -> bool:
    if not origin:
        return False
    if origin in CFG.CORS_ALLOWED_ORIGINS:
        return True
    if _CORS_REGEX.match(origin):
        return True
    # السماح أيضاً لنفس الـ backend origin (لاختبار curl بدون Origin)
    return False

# Flask-CORS معطل افتراضياً — نتحكم يدوياً في after_request لضمان Regex + قائمة دقيقة
# نحتفظ بـ CORS كـ fallback للـ preflight لكن origins محدد
CORS(app, resources={
    r"/api/*": {"origins": CFG.CORS_ALLOWED_ORIGINS, "methods": ["GET", "OPTIONS"], "allow_headers": ["Content-Type", "X-Request-ID"]},
    r"/health": {"origins": CFG.CORS_ALLOWED_ORIGINS, "methods": ["GET", "OPTIONS"]},
    r"/": {"origins": CFG.CORS_ALLOWED_ORIGINS},
})

# Rate limiter — memory افتراضياً، Redis إن وجد REDIS_URL
_redis_url = os.environ.get("REDIS_URL")
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=_redis_url if _redis_url else "memory://",
    default_limits=["60 per hour", "20 per minute"],
    headers_enabled=True,
)

_CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", CFG.CACHE_TTL_SECONDS))
_CACHE_FILE = pathlib.Path("/tmp/gscsi_cache.db")
_redis_client = None
if _redis_url:
    try:
        import redis
        _redis_client = redis.from_url(_redis_url, socket_connect_timeout=2)
        _redis_client.ping()
        logger.info("Redis cache enabled: %s", _redis_url.split("@")[-1])
    except Exception as e:
        logger.warning("Redis unavailable, fallback to file cache: %s", e)
        _redis_client = None

def _cache_get(key: tuple):
    k = f"gscsi:{key[0]}:{key[1]}:{key[2]}"
    try:
        if _redis_client:
            import json as _j
            v = _redis_client.get(k)
            if v:
                obj = _j.loads(v)
                if time.time() - obj["ts"] < _CACHE_TTL_SECONDS:
                    return obj["data"]
        else:
            with shelve.open(str(_CACHE_FILE)) as db:
                obj = db.get(k)
                if obj and time.time() - obj["ts"] < _CACHE_TTL_SECONDS:
                    return obj["data"]
    except Exception as e:
        logger.warning("cache_get failed %s: %s", k, e)
    return None

def _cache_set(key: tuple, data: dict):
    k = f"gscsi:{key[0]}:{key[1]}:{key[2]}"
    try:
        if _redis_client:
            import json as _j
            _redis_client.setex(k, _CACHE_TTL_SECONDS, _j.dumps({"ts": time.time(), "data": data}))
        else:
            with shelve.open(str(_CACHE_FILE)) as db:
                db[k] = {"ts": time.time(), "data": data}
    except Exception as e:
        logger.warning("cache_set failed %s: %s", k, e)

# --- Pydantic validation ---
class GSCSIQuery(BaseModel):
    year: int = Field(default=2026, ge=CFG.YEAR_MIN, le=CFG.YEAR_MAX)
    start_month: int = Field(default=6, ge=1, le=12)
    end_month: int = Field(default=10, ge=1, le=12)

    @field_validator("end_month", mode="after")
    @classmethod
    def check_range(cls, v, info):
        sm = info.data.get("start_month")
        if sm is not None and v < sm:
            raise ValueError("end_month يجب أن يكون >= start_month")
        return v

@app.before_request
def _set_request_id():
    g.request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:8])

@app.after_request
def _add_headers(resp):
    resp.headers["X-Request-ID"] = g.get("request_id", "")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # CORS ديناميكي — نستبدل/نزيل الهيدر حسب القائمة + Regex
    origin = request.headers.get("Origin")
    # أولاً: احذف أي هيدر وضعه Flask-CORS افتراضياً لنعيد ضبطه
    resp.headers.pop("Access-Control-Allow-Origin", None)
    if origin and _is_origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Request-ID"
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Credentials"] = "false"
    elif origin:
        # origin غير مسموح — نحذف الهيدر تماماً ليحظره المتصفح (لا نرسل *)
        resp.headers.pop("Access-Control-Allow-Origin", None)
        # لا نضيفه — المتصفح سيرفض
        pass
    else:
        # طلب بدون Origin (curl/health check/monitors) — لا حاجة لـ CORS
        pass
    return resp

@app.route("/health", methods=["GET", "OPTIONS"])
def health_alias():
    """Alias لـ /api/health — للـ uptime monitors التي تتوقع /health"""
    return health()

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "GSCSI Agriculture API", "docs": "/api/health", "health": "/health", "frontend": CFG.PROD_BACKEND_URL})

@app.before_request
def _handle_preflight():
    if request.method == "OPTIONS":
        origin = request.headers.get("Origin")
        if origin and not _is_origin_allowed(origin) and origin != "*":
            # لا نكسر — نترك after_request يضبط الهيدر، لكن نعيد 204 سريع
            pass
        resp = jsonify({"ok": True})
        resp.status_code = 204
        return resp

@app.route("/api/health", methods=["GET", "OPTIONS"])
def health():
    gee_status = "unknown"
    try:
        gscsi_engine.init_earth_engine()
        import ee
        ee.Number(1).getInfo()
        gee_status = "ok"
    except Exception as e:
        gee_status = f"error:{type(e).__name__}"
        logger.warning("health GEE check failed: %s", e)
    return jsonify({
        "status": "ok" if gee_status == "ok" else "degraded",
        "gee": gee_status,
        "cache_backend": "redis" if _redis_client else "file",
        "time": datetime.datetime.utcnow().isoformat() + "Z",
        "request_id": g.request_id,
    })

@app.route("/api/gscsi", methods=["GET"])
@limiter.limit("5 per minute")
def get_gscsi():
    # تحقق صارم
    try:
        q = GSCSIQuery(
            year=int(request.args.get("year", 2026)),
            start_month=int(request.args.get("start_month", 6)),
            end_month=int(request.args.get("end_month", 10)),
        )
    except (ValueError, ValidationError) as e:
        return jsonify({"error": "معاملات غير صالحة", "details": str(e), "request_id": g.request_id}), 400

    cache_key = (q.year, q.start_month, q.end_month)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info("Cache HIT %s rid=%s", cache_key, g.request_id)
        return jsonify(cached)

    try:
        logger.info("Computing GSCSI %s rid=%s", cache_key, g.request_id)
        data = gscsi_engine.run_pipeline(q.year, q.start_month, q.end_month)
        data["request_id"] = g.request_id
        _cache_set(cache_key, data)
        return jsonify(data)
    except Exception as e:
        rid = g.request_id
        logger.error("Pipeline failed rid=%s key=%s err=%s\n%s", rid, cache_key, e, traceback.format_exc())
        # لا تسرب str(e) الداخلي كاملاً للعميل
        return jsonify({"error": "فشل حساب المؤشر، حاول لاحقاً", "request_id": rid}), 500

# --- LOYO async jobs (نفس النمط لكن مع قفل + cache مشترك) ---
_LOYO_JOBS: dict = {}
_LOYO_LOCK = threading.Lock()

def _loyo_key(crop: str, sector: str): return (crop, sector)
def _sens_key(crop: str, sector: str): return ("sensitivity", crop, sector)

def _run_loyo_bg(crop, sector):
    key = _loyo_key(crop, sector)
    try:
        logger.info("LOYO bg start %s", key)
        result = gscsi_engine.run_loyo_validation(crop=crop, sector=sector)
        with _LOYO_LOCK:
            _LOYO_JOBS[key] = {"status": "done", "result": result, "error": None, "finished_at": time.time()}
    except Exception as e:
        logger.error("LOYO bg failed %s: %s\n%s", key, e, traceback.format_exc())
        with _LOYO_LOCK:
            _LOYO_JOBS[key] = {"status": "error", "result": None, "error": "فشل الحساب الداخلي", "finished_at": time.time()}

@app.route("/api/validation/loyo/start", methods=["GET"])
@limiter.limit("2 per hour")
def start_loyo_validation():
    crop = request.args.get("crop", "sorghum")
    if crop not in ("sorghum", "sesame"):
        return jsonify({"error": "crop يجب sorghum أو sesame", "request_id": g.request_id}), 400
    default_sector = "mechanized_rainfed" if crop == "sesame" else "semi_mechanized"
    sector = request.args.get("sector", default_sector)
    key = _loyo_key(crop, sector)
    with _LOYO_LOCK:
        ex = _LOYO_JOBS.get(key)
        if ex and ex["status"] == "running":
            return jsonify({"status": "already_running", "poll_url": f"/api/validation/loyo/status?crop={crop}&sector={sector}", "request_id": g.request_id})
        if ex and ex["status"] == "done" and time.time() - ex["finished_at"] < _CACHE_TTL_SECONDS:
            return jsonify({"status": "done", "cached": True, "result": ex["result"], "request_id": g.request_id})
        _LOYO_JOBS[key] = {"status": "running", "result": None, "error": None, "started_at": time.time()}
    threading.Thread(target=_run_loyo_bg, args=(crop, sector), daemon=True).start()
    return jsonify({"status": "started", "message": "بدأ الحساب (3-7 دقائق)", "poll_url": f"/api/validation/loyo/status?crop={crop}&sector={sector}", "crop": crop, "sector": sector, "request_id": g.request_id})

@app.route("/api/validation/loyo/status", methods=["GET"])
def get_loyo_status():
    crop = request.args.get("crop", "sorghum")
    default_sector = "mechanized_rainfed" if crop == "sesame" else "semi_mechanized"
    sector = request.args.get("sector", default_sector)
    key = _loyo_key(crop, sector)
    with _LOYO_LOCK:
        job = _LOYO_JOBS.get(key)
    if job is None:
        return jsonify({"status": "not_started", "start_url": f"/api/validation/loyo/start?crop={crop}&sector={sector}", "request_id": g.request_id})
    if job["status"] == "running":
        return jsonify({"status": "running", "elapsed_seconds": round(time.time() - job["started_at"], 1), "request_id": g.request_id})
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"], "request_id": g.request_id}), 500
    return jsonify({"status": "done", "result": job["result"], "request_id": g.request_id})

@app.route("/api/validation/sensitivity/start", methods=["GET"])
@limiter.limit("2 per hour")
def start_sensitivity():
    crop = request.args.get("crop", "sorghum")
    default_sector = "mechanized_rainfed" if crop == "sesame" else "semi_mechanized"
    sector = request.args.get("sector", default_sector)
    key = _sens_key(crop, sector)
    with _LOYO_LOCK:
        ex = _LOYO_JOBS.get(key)
        if ex and ex["status"] == "running":
            return jsonify({"status": "already_running", "poll_url": f"/api/validation/sensitivity/status?crop={crop}&sector={sector}"})
        if ex and ex["status"] == "done" and time.time() - ex["finished_at"] < _CACHE_TTL_SECONDS:
            return jsonify({"status": "done", "cached": True, "result": ex["result"]})
        _LOYO_JOBS[key] = {"status": "running", "result": None, "error": None, "started_at": time.time()}
    def _run():
        try:
            r = gscsi_engine.run_weight_sensitivity_analysis(crop=crop, sector=sector)
            with _LOYO_LOCK: _LOYO_JOBS[key] = {"status": "done", "result": r, "error": None, "finished_at": time.time()}
        except Exception as e:
            logger.error("Sensitivity failed: %s\n%s", e, traceback.format_exc())
            with _LOYO_LOCK: _LOYO_JOBS[key] = {"status": "error", "result": None, "error": "فشل الحساب", "finished_at": time.time()}
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started", "poll_url": f"/api/validation/sensitivity/status?crop={crop}&sector={sector}", "crop": crop, "sector": sector})

@app.route("/api/validation/sensitivity/status", methods=["GET"])
def get_sensitivity_status():
    crop = request.args.get("crop", "sorghum")
    default_sector = "mechanized_rainfed" if crop == "sesame" else "semi_mechanized"
    sector = request.args.get("sector", default_sector)
    key = _sens_key(crop, sector)
    with _LOYO_LOCK: job = _LOYO_JOBS.get(key)
    if job is None: return jsonify({"status": "not_started", "start_url": f"/api/validation/sensitivity/start?crop={crop}&sector={sector}"})
    if job["status"] == "running": return jsonify({"status": "running", "elapsed_seconds": round(time.time()-job["started_at"],1)})
    if job["status"] == "error": return jsonify({"status": "error", "error": job["error"]}), 500
    return jsonify({"status": "done", "result": job["result"]})

@app.errorhandler(429)
def ratelimit(e):
    return jsonify({"error": "تجاوزت الحد المسموح، حاول لاحقاً", "details": str(e.description), "request_id": g.get("request_id","")}), 429

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
