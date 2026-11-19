# -*- coding: utf-8 -*-
"""
tests/test_gscsi_engine.py — اختبارات وحدة شاملة لـ GSCSI وواجهة Flask بدون الحاجة لـ GEE فعلي (Mocked)
تغطي: الأوزان، تطبيع المؤشرات، tenacity retry، fc_to_df، tile_url، run_pipeline،
حالات التواريخ الحدّية (YTD)، مسارات /health و /api/health، CORS/OPTIONS، وأمان الكاش للخيوط.
"""
import datetime
import json
import pathlib as pl
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1) اختبارات الإعدادات والمنطق الرياضي
# ---------------------------------------------------------------------------
def test_weights_sum_is_one():
    import backend.config as CFG
    s = sum(CFG.GSCSI_WEIGHTS.values())
    assert abs(s - 1.0) < 1e-9, f"مجموع الأوزان {s} != 1.0"
    for k, v in CFG.GSCSI_WEIGHTS.items():
        assert 0 <= v <= 1, f"{k} خارج [0,1]"


def test_weight_variants_sum_one_and_single_source():
    import backend.config as CFG
    import backend.gscsi_engine as eng

    assert eng.WEIGHT_VARIANTS is CFG.WEIGHT_VARIANTS
    for name, w in CFG.WEIGHT_VARIANTS.items():
        assert abs(sum(w.values()) - 1.0) < 1e-9, f"variant {name} sum != 1"


def test_normalize_logic():
    """يختبر منطق normalize_band بدون GEE — clamping 0-100."""
    def normalize(val, vmin, vmax, invert=False):
        norm = (val - vmin) / (vmax - vmin) * 100
        norm = max(0, min(100, norm))
        if invert:
            norm = 100 - norm
        return norm

    assert normalize(0, -40, 40) == 50
    assert normalize(100, -40, 40) == 100
    assert normalize(-100, -40, 40) == 0
    assert normalize(0, -3, 3, invert=True) == 50


def test_gscsi_weighted_sum():
    import backend.config as CFG
    comps = {k: 50 for k in CFG.GSCSI_WEIGHTS}
    gscsi = sum(comps[k] * w for k, w in CFG.GSCSI_WEIGHTS.items())
    assert abs(gscsi - 50) < 1e-9
    comps2 = {k: 100 for k in CFG.GSCSI_WEIGHTS}
    assert abs(sum(comps2[k] * w for k, w in CFG.GSCSI_WEIGHTS.items()) - 100) < 1e-9


def test_ground_truth_json_schema():
    p = pl.Path("backend/data/gedaref_ground_truth_yields.json")
    assert p.exists(), "ملف ground truth مفقود"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "records" in data
    for r in data["records"]:
        assert "season_year" in r and "crop" in r and "sector" in r
        assert r["crop"] in ("sorghum", "sesame")
        if r["yield_t_ha"] is not None:
            assert 0 < r["yield_t_ha"] < 5, f"yield غير منطقي {r}"


def test_pearson_and_fit():
    from backend.gscsi_engine import _pearson_r, _fit_linear
    assert abs(_pearson_r([1, 2, 3], [1, 2, 3]) - 1.0) < 1e-9
    assert _pearson_r([1, 1, 1], [1, 2, 3]) is None
    slope, intercept = _fit_linear([1, 2, 3], [2, 4, 6])
    assert abs(slope - 2) < 1e-9 and abs(intercept) < 1e-9


def test_config_onset_thresholds():
    import backend.config as CFG
    assert CFG.ONSET["min_mm"] > 0
    assert CFG.DRY_DEKAD_THRESHOLD_MM > 0
    assert CFG.FLOWERING["start_offset_days"] < CFG.FLOWERING["end_offset_days"]


def test_api_query_validation():
    from backend.app import GSCSIQuery
    from pydantic import ValidationError

    q = GSCSIQuery(year=2024, start_month=6, end_month=10)
    assert q.year == 2024

    with pytest.raises(ValidationError):
        GSCSIQuery(year=2024, start_month=10, end_month=6)

    with pytest.raises(ValidationError):
        GSCSIQuery(year=2050, start_month=6, end_month=10)


# ---------------------------------------------------------------------------
# 2) اختبارات GEE Wrappers (tenacity retry, fc_to_df, tile_url)
# ---------------------------------------------------------------------------
def test_gee_get_info_retries_transient_error():
    import backend.gscsi_engine as eng

    ee_obj = MagicMock()
    ee_obj.getInfo.side_effect = [ConnectionError("transient"), {"ok": True}]

    with patch("time.sleep", return_value=None):
        result = eng._gee_get_info(ee_obj)

    assert result == {"ok": True}
    assert ee_obj.getInfo.call_count == 2


def test_gee_get_info_does_not_retry_value_error():
    import backend.gscsi_engine as eng

    ee_obj = MagicMock()
    ee_obj.getInfo.side_effect = ValueError("bad input")

    with pytest.raises(ValueError):
        eng._gee_get_info(ee_obj)

    assert ee_obj.getInfo.call_count == 1


def test_fc_to_df_uses_gee_get_info():
    import backend.gscsi_engine as eng

    fake_fc = MagicMock()
    fake_fc.getInfo.return_value = {
        "features": [
            {"properties": {"ADM2_NAME": "Al Faw", "GSCSI": 72.5}},
            {"properties": {"ADM2_NAME": "Al Gadaref", "GSCSI": 68.1}},
        ]
    }
    df = eng.fc_to_df(fake_fc)
    assert list(df["ADM2_NAME"]) == ["Al Faw", "Al Gadaref"]
    assert list(df["GSCSI"]) == [72.5, 68.1]
    fake_fc.getInfo.assert_called_once()


def test_tile_url_uses_gee_get_map_id():
    import backend.gscsi_engine as eng

    fake_fetcher = MagicMock()
    fake_fetcher.url_format = "https://earthengine.googleapis.com/v1/tiles/{z}/{x}/{y}"
    fake_img = MagicMock()
    fake_img.getMapId.return_value = {"tile_fetcher": fake_fetcher}

    url = eng.tile_url(fake_img, {"min": 0, "max": 100})
    assert url == fake_fetcher.url_format
    fake_img.getMapId.assert_called_once_with({"min": 0, "max": 100})


# ---------------------------------------------------------------------------
# 3) اختبارات حالات التواريخ الحدّية (YTD)
# ---------------------------------------------------------------------------
def test_resolve_season_dates_rejects_future_or_not_started():
    from backend.gscsi_engine import _resolve_season_dates

    # السنة الحالية لكن التاريخ قبل شهر بداية الموسم (أبريل < يونيو)
    with pytest.raises(ValueError, match="لم يبدأ بعد"):
        _resolve_season_dates(2026, 6, 10, today=datetime.date(2026, 4, 15))

    # يوم بداية الموسم نفسه بدون أيام فعلية مكتملة
    with pytest.raises(ValueError, match="لم يبدأ بعد"):
        _resolve_season_dates(2026, 6, 10, today=datetime.date(2026, 6, 1))

    # سنة مستقبلية
    with pytest.raises(ValueError, match="لم يبدأ بعد"):
        _resolve_season_dates(2027, 6, 10, today=datetime.date(2026, 11, 19))


def test_resolve_season_dates_ytd_and_completed():
    from backend.gscsi_engine import _resolve_season_dates

    # منتصف الموسم الجاري → YTD
    start, end, is_ytd = _resolve_season_dates(2026, 6, 10, today=datetime.date(2026, 8, 20))
    assert start == datetime.date(2026, 6, 1)
    assert end == datetime.date(2026, 8, 20)
    assert is_ytd is True

    # بعد نهاية الموسم (نوفمبر > أكتوبر) → مكتمل
    start2, end2, is_ytd2 = _resolve_season_dates(2026, 6, 10, today=datetime.date(2026, 11, 19))
    assert start2 == datetime.date(2026, 6, 1)
    assert end2 == datetime.date(2026, 11, 1)
    assert is_ytd2 is False


# ---------------------------------------------------------------------------
# 4) اختبار run_pipeline بالكامل مع محاكاة Google Earth Engine
# ---------------------------------------------------------------------------
class _ChainableEE:
    """كائن وهمي قابل للتسلسل يحاكي كائنات ee (Image, ImageCollection, List, Dictionary)."""
    def __init__(self, payload=None):
        self._payload = payload

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            return _ChainableEE()
        return _method

    def __call__(self, *args, **kwargs):
        return _ChainableEE()


def test_run_pipeline_mocked_end_to_end():
    import backend.config as CFG
    import backend.gscsi_engine as eng

    fake_ee = MagicMock()
    fake_ee.Date.fromYMD.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Image.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Image.cat.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.ImageCollection.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.ImageCollection.fromImages.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.FeatureCollection.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Filter.lt.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Filter.eq.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Filter.calendarRange.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.List.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.List.sequence.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Number.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Reducer.mean.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Reducer.stdDev.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Algorithms.If.side_effect = lambda *a, **k: _ChainableEE()
    fake_ee.Dictionary.side_effect = lambda d: _ChainableEE(payload=d)

    # تسلسل استدعاءات _gee_get_info المتوقع لموسم مكتمل (2024):
    # 1) onset_matrix
    # 2) flowering_rain dict
    # 3) zonal_stats_df (cropland فقط لأن كل المحليات بها بيانات صالحة)
    # 4) ndvi_timeseries dict (مدمج في استدعاء واحد)
    # 5) summary + centroid dict (مدمج في استدعاء واحد)
    onset_years_count = 2024 - 1991 + 1
    fake_onset_row = [5.0, 10.0, 25.0, 15.0, 10.0] + [12.0] * 13
    summary_masked = {"GSCSI": 72.4, "precipitation": 510.2, "Rainfall_Anomaly_Pct": 8.5}
    for k in CFG.GSCSI_WEIGHTS:
        summary_masked[k] = 70.0

    get_info_responses = [
        [fake_onset_row for _ in range(onset_years_count)],
        {"current": 140.0, "hist": 120.0},
        {
            "features": [
                {
                    "properties": {
                        "ADM2_NAME": "Al Faw",
                        "GSCSI": 74.2,
                        "GSCSI_Class": 3.0,
                        "Rain_Season_mm": 520.0,
                        "Rainfall_Anomaly_Pct": 9.1,
                        "NDVI": 0.42,
                        "NDMI": 0.08,
                        "VCI": 78.0,
                        "TCI": 81.0,
                    }
                }
            ]
        },
        {
            "current_dates": ["2024-07-01", "2024-08-01"],
            "current_vals": [3500, 5200],
            "hist_dates": ["2021-07-01", "2022-07-01", "2021-08-01", "2022-08-01"],
            "hist_vals": [3200, 3400, 4900, 5100],
        },
        {
            "masked": summary_masked,
            "centroid": [35.4, 14.0],
        },
    ]

    fake_fetcher = MagicMock()
    fake_fetcher.url_format = "https://tiles.example/{z}/{x}/{y}"

    with (
        patch.object(eng, "ee", fake_ee),
        patch.object(eng, "init_earth_engine", return_value=None),
        patch.object(eng, "load_gedaref_boundaries", return_value=(_ChainableEE(), _ChainableEE(), _ChainableEE())),
        patch.object(eng, "load_cropland_mask", return_value=_ChainableEE()),
        patch.object(eng, "_gee_get_info", side_effect=get_info_responses) as mock_get_info,
        patch.object(eng, "_gee_get_map_id", return_value={"tile_fetcher": fake_fetcher}) as mock_map_id,
    ):
        res = eng.run_pipeline(2024, 6, 10)

    # تأكد أن استدعاءات _gee_get_info المجمّعة = 5 فقط (بدلاً من 20+ استدعاء سابقاً)
    assert mock_get_info.call_count == 5
    assert mock_map_id.call_count == 4
    assert res["meta"]["season_year"] == 2024
    assert res["meta"]["is_ytd"] is False
    assert res["summary"]["gscsi_mean"] == 72.4
    assert res["summary"]["gscsi_class_label"] == "متوسط"
    assert res["summary"]["rain_season_mm"] == 510.2
    assert len(res["summary"]["component_scores"]) == len(CFG.GSCSI_WEIGHTS)
    assert res["map"]["center"] == [14.0, 35.4]
    assert set(res["map"]["tiles"].keys()) == {"gscsi", "class", "ndvi", "rainfall_anomaly"}
    assert len(res["localities"]) == 1
    assert res["localities"][0]["ADM2_NAME"] == "Al Faw"
    assert len(res["ndvi_timeseries"]["current"]) == 2
    assert len(res["ndvi_timeseries"]["historical"]) == 2
    assert res["phenology"]["onset_date"] == "2024-04-21"


# ---------------------------------------------------------------------------
# 5) اختبارات خادم Flask (/health، /api/health، CORS/OPTIONS، أمان الكاش للخيوط)
# ---------------------------------------------------------------------------
def test_health_endpoint_is_fast_and_does_not_call_gee():
    import backend.app as api

    client = api.app.test_client()
    with patch.object(api.gscsi_engine, "init_earth_engine") as mock_init:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    mock_init.assert_not_called()


def test_api_health_caches_gee_check():
    import backend.app as api

    api._GEE_HEALTH_STATE["ts"] = 0.0
    api._GEE_HEALTH_STATE["gee"] = "unknown"

    client = api.app.test_client()
    with (
        patch.object(api.gscsi_engine, "init_earth_engine", return_value=None) as mock_init,
        patch("ee.Number", return_value=_ChainableEE()),
        patch.object(api.gscsi_engine, "_gee_get_info", return_value=1) as mock_info,
    ):
        r1 = client.get("/api/health")
        r2 = client.get("/api/health")

    assert r1.status_code == 200
    assert r1.get_json()["gee"] == "ok"
    assert r1.get_json()["gee_cached"] is False
    assert r2.status_code == 200
    assert r2.get_json()["gee_cached"] is True
    assert mock_init.call_count == 1
    assert mock_info.call_count == 1


def test_cors_and_options_preflight():
    import backend.app as api

    client = api.app.test_client()
    allowed_origin = "https://75nn12345872168.github.io"
    r_ok = client.options("/api/gscsi", headers={"Origin": allowed_origin})
    assert r_ok.status_code == 204
    assert r_ok.data == b""
    assert r_ok.headers.get("Access-Control-Allow-Origin") == allowed_origin
    assert "Access-Control-Allow-Credentials" not in r_ok.headers

    r_bad = client.options("/api/gscsi", headers={"Origin": "https://evil.example.com"})
    assert r_bad.status_code == 204
    assert "Access-Control-Allow-Origin" not in r_bad.headers


def test_file_cache_is_thread_safe(tmp_path):
    import backend.app as api

    orig_file = api._CACHE_FILE
    orig_redis = api._redis_client
    api._CACHE_FILE = tmp_path / "thread_safe_cache.db"
    api._redis_client = None
    errors = []

    def worker(idx: int):
        try:
            key = (2020 + (idx % 4), 6, 10)
            api._cache_set(key, {"idx": idx, "ok": True})
            val = api._cache_get(key)
            assert val is not None and val["ok"] is True
        except Exception as e:
            errors.append(e)

    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        api._CACHE_FILE = orig_file
        api._redis_client = orig_redis

    assert errors == [], f"حدثت أخطاء تزامن في الكاش: {errors}"
