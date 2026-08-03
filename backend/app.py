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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
