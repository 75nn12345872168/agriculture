# -*- coding: utf-8 -*-
"""
app.py — واجهة REST API لنظام GSCSI (نظام الإنذار المبكر الزراعي — ولاية القضارف)

التشغيل محلياً:
    pip install -r requirements.txt
    export GEE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
    python app.py

النشر: انظر README.md لخطوات النشر على Render / Railway / Cloud Run.
"""

import os
import time
import logging
import traceback
import threading

from flask import Flask, jsonify, request
from flask_cors import CORS

import gscsi_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gscsi-api")

app = Flask(__name__)
CORS(app)  # يسمح لواجهة الويب (GitHub Pages مثلاً) بالاتصال بهذا الـ API من دومين مختلف

# كاش بسيط في الذاكرة: حساب GEE مكلف زمنياً (قد يأخذ 20-60 ثانية)، فلا داعي لإعادة حسابه
# لكل زائر لنفس الموسم خلال فترة قصيرة.
_CACHE = {}
_CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", 6 * 60 * 60))  # 6 ساعات افتراضياً


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/gscsi", methods=["GET"])
def get_gscsi():
    try:
        season_year = int(request.args.get("year", 2026))
        start_month = int(request.args.get("start_month", 6))
        end_month = int(request.args.get("end_month", 10))
    except ValueError:
        return jsonify({"error": "معاملات غير صالحة. تأكد أن year/start_month/end_month أرقام صحيحة."}), 400

    cache_key = (season_year, start_month, end_month)
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"] < _CACHE_TTL_SECONDS):
        logger.info("Cache hit for %s", cache_key)
        return jsonify(cached["data"])

    try:
        logger.info("Computing GSCSI pipeline for %s ...", cache_key)
        data = gscsi_engine.run_pipeline(season_year, start_month, end_month)
        _CACHE[cache_key] = {"ts": time.time(), "data": data}
        return jsonify(data)
    except Exception as e:
        logger.error("Pipeline failed: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": f"فشل حساب المؤشر: {str(e)}"}), 500


####################################################################
# LOYO Validation — نمط "شغّل في الخلفية ثم استعلم" (Start/Poll)
####################################################################
# سبب هذا التصميم: حساب GSCSI لـ 7 سنوات عبر GEE بشكل متتابع قد يأخذ
# 3-7 دقائق، وهو ما يتجاوز مهلة Gunicorn worker (--timeout) ويؤدي إلى
# SystemExit وانهيار العامل قبل انتهاء الطلب. الحل: /start يشغّل الحساب
# في thread منفصل ويرجع فوراً، و/status يُستعلم دورياً لمعرفة النتيجة.

_LOYO_JOBS = {}          # key: (crop, sector) -> {"status", "result", "error", "started_at"}
_LOYO_JOBS_LOCK = threading.Lock()


def _loyo_job_key(crop, sector):
    return (crop, sector)


def _run_loyo_job_in_background(crop, sector):
    key = _loyo_job_key(crop, sector)
    try:
        logger.info("LOYO background job started for %s", key)
        result = gscsi_engine.run_loyo_validation(crop=crop, sector=sector)
        with _LOYO_JOBS_LOCK:
            _LOYO_JOBS[key] = {
                "status": "done",
                "result": result,
                "error": None,
                "finished_at": time.time(),
            }
        logger.info("LOYO background job finished for %s", key)
    except Exception as e:
        logger.error("LOYO background job failed for %s: %s\n%s", key, e, traceback.format_exc())
        with _LOYO_JOBS_LOCK:
            _LOYO_JOBS[key] = {
                "status": "error",
                "result": None,
                "error": str(e),
                "finished_at": time.time(),
            }


@app.route("/api/validation/loyo/start", methods=["GET"])
def start_loyo_validation():
    """
    يبدأ حساب LOYO في الخلفية ويرجع فوراً (لا ينتظر انتهاء الحساب).
    Query params: crop (sorghum افتراضي | sesame), sector (اختياري)

    استدعِ /api/validation/loyo/status بنفس الـ crop/sector للمتابعة.
    """
    crop = request.args.get("crop", "sorghum")
    default_sector = "mechanized_rainfed" if crop == "sesame" else "semi_mechanized"
    sector = request.args.get("sector", default_sector)
    key = _loyo_job_key(crop, sector)

    with _LOYO_JOBS_LOCK:
        existing = _LOYO_JOBS.get(key)
        if existing and existing["status"] == "running":
            return jsonify({
                "status": "already_running",
                "message": "الحساب شغال بالفعل، استخدم /status للمتابعة",
                "poll_url": f"/api/validation/loyo/status?crop={crop}&sector={sector}",
            })
        if existing and existing["status"] == "done":
            # لو النتيجة محسوبة ومخزنة أصلاً ولسه ضمن مدة الكاش، نرجعها فورًا
            age = time.time() - existing["finished_at"]
            if age < _CACHE_TTL_SECONDS:
                return jsonify({
                    "status": "done",
                    "cached": True,
                    "result": existing["result"],
                })

        _LOYO_JOBS[key] = {"status": "running", "result": None, "error": None, "started_at": time.time()}

    thread = threading.Thread(target=_run_loyo_job_in_background, args=(crop, sector), daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "message": "بدأ الحساب في الخلفية. قد يأخذ 3-7 دقائق (سنة بسنة عبر GEE).",
        "poll_url": f"/api/validation/loyo/status?crop={crop}&sector={sector}",
        "crop": crop,
        "sector": sector,
    })


@app.route("/api/validation/loyo/status", methods=["GET"])
def get_loyo_validation_status():
    """
    يرجّع حالة/نتيجة حساب LOYO الذي بدأ عبر /start.
    status ممكن تكون: not_started | running | done | error
    """
    crop = request.args.get("crop", "sorghum")
    default_sector = "mechanized_rainfed" if crop == "sesame" else "semi_mechanized"
    sector = request.args.get("sector", default_sector)
    key = _loyo_job_key(crop, sector)

    with _LOYO_JOBS_LOCK:
        job = _LOYO_JOBS.get(key)

    if job is None:
        return jsonify({
            "status": "not_started",
            "message": "لم يبدأ أي حساب بعد لهذا الـ crop/sector. استدعِ /start أولاً.",
            "start_url": f"/api/validation/loyo/start?crop={crop}&sector={sector}",
        })

    if job["status"] == "running":
        elapsed = round(time.time() - job["started_at"], 1)
        return jsonify({
            "status": "running",
            "elapsed_seconds": elapsed,
            "message": "لسه بيحسب... جرّب تاني بعد شوية.",
        })

    if job["status"] == "error":
        return jsonify({
            "status": "error",
            "error": job["error"],
        }), 500

    # status == "done"
    return jsonify({
        "status": "done",
        "result": job["result"],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
