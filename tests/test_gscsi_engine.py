# -*- coding: utf-8 -*-
"""
tests/test_gscsi_engine.py — اختبارات رياضية لـ GSCSI بدون الحاجة لـ GEE (Mocked)
تغطي: مجموع الأوزان، تطبيع المؤشرات، LOYO، وحساسية الأوزان
"""
import math
import json
import pathlib as pl

def test_weights_sum_is_one():
    import backend.config as CFG
    s = sum(CFG.GSCSI_WEIGHTS.values())
    assert abs(s - 1.0) < 1e-9, f"مجموع الأوزان {s} != 1.0"
    # كل وزن بين 0 و1
    for k,v in CFG.GSCSI_WEIGHTS.items():
        assert 0 <= v <= 1, f"{k} خارج [0,1]"

def test_weight_variants_sum_one():
    import backend.config as CFG
    for name, w in CFG.WEIGHT_VARIANTS.items():
        assert abs(sum(w.values())-1.0) < 1e-9, f"variant {name} sum !=1"

def test_normalize_logic():
    """يختبر منطق normalize_band بدون GEE — clamping 0-100"""
    def normalize(val, vmin, vmax, invert=False):
        norm = (val - vmin) / (vmax - vmin) * 100
        norm = max(0, min(100, norm))
        if invert: norm = 100 - norm
        return norm
    assert normalize(0, -40, 40) == 50
    assert normalize(100, -40, 40) == 100  # clamp
    assert normalize(-100, -40, 40) == 0   # clamp
    assert normalize(0, -3, 3, invert=True) == 50

def test_gscsi_weighted_sum():
    import backend.config as CFG
    comps = {k: 50 for k in CFG.GSCSI_WEIGHTS}
    gscsi = sum(comps[k]*w for k,w in CFG.GSCSI_WEIGHTS.items())
    assert abs(gscsi - 50) < 1e-9
    comps2 = {k: 100 for k in CFG.GSCSI_WEIGHTS}
    assert abs(sum(comps2[k]*w for k,w in CFG.GSCSI_WEIGHTS.items()) - 100) < 1e-9

def test_ground_truth_json_schema():
    p = pl.Path("backend/data/gedaref_ground_truth_yields.json")
    assert p.exists(), "ملف ground truth مفقود"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "records" in data
    for r in data["records"]:
        assert "season_year" in r and "crop" in r and "sector" in r
        assert r["crop"] in ("sorghum","sesame")
        # yield قد يكون null لسنة غير مكتملة
        if r["yield_t_ha"] is not None:
            assert 0 < r["yield_t_ha"] < 5, f"yield غير منطقي {r}"

def test_pearson_and_fit():
    from backend.gscsi_engine import _pearson_r, _fit_linear
    # ارتباط مثالي
    assert abs(_pearson_r([1,2,3],[1,2,3]) - 1.0) < 1e-9
    assert _pearson_r([1,1,1],[1,2,3]) is None  # تباين صفر
    slope, intercept = _fit_linear([1,2,3],[2,4,6])
    assert abs(slope - 2) < 1e-9 and abs(intercept) < 1e-9

def test_config_onset_thresholds():
    import backend.config as CFG
    assert CFG.ONSET["min_mm"] > 0
    assert CFG.DRY_DEKAD_THRESHOLD_MM > 0
    assert CFG.FLOWERING["start_offset_days"] < CFG.FLOWERING["end_offset_days"]

def test_api_query_validation():
    from backend.app import GSCSIQuery
    from pydantic import ValidationError
    # صحيح
    q = GSCSIQuery(year=2024, start_month=6, end_month=10)
    assert q.year == 2024
    # خطأ: end قبل start
    try:
        GSCSIQuery(year=2024, start_month=10, end_month=6)
        assert False, "يجب أن يفشل"
    except ValidationError:
        pass
    # خطأ: سنة خارج النطاق
    try:
        GSCSIQuery(year=2050, start_month=6, end_month=10)
        assert False
    except ValidationError:
        pass
