# -*- coding: utf-8 -*-
"""
config.py — مصدر الحقيقة الوحيد لكل عتبات ومعاملات GSCSI
كل وزن/عتبة هنا موثق ومجموع الأوزان متحقق منه تلقائياً.
"""
from typing import Dict, Final

GSCSI_WEIGHTS: Final[Dict[str, float]] = {
    "NDVI_Anomaly_Score": 0.17,
    "Rainfall_Anomaly_Score": 0.17,
    "VHI_Score": 0.15,
    "SPEI_Score": 0.10,
    "SoilMoisture_Score": 0.10,
    "LST_Score": 0.13,
    "NDMI_Score": 0.04,
    "LAI_Score": 0.04,
    "Dry_Spell_Score": 0.10,
}
# فحص مجموع الأوزان فور الاستيراد — يفشل الـ import لو خطأ
assert abs(sum(GSCSI_WEIGHTS.values()) - 1.0) < 1e-9, (
    f"مجموع أوزان GSCSI يجب =1.0 وجد {sum(GSCSI_WEIGHTS.values())}"
)

# كشف بداية الموسم (dekad)
ONSET: Final[Dict[str, int]] = {
    "search_start_month": 4,
    "n_dekads": 18,
    "min_mm": 20,
    "confirm_dekads": 2,
    "confirm_min_mm": 20,
}

# نافذة الإزهار FAO-56 للذرة الرفيعة
FLOWERING: Final[Dict[str, int]] = {
    "start_offset_days": 55,
    "end_offset_days": 95,
}

DRY_DEKAD_THRESHOLD_MM: Final[float] = 5.0

CACHE_TTL_SECONDS: Final[int] = 6 * 60 * 60  # 6 ساعات
GEE_HEALTH_CACHE_TTL_SECONDS: Final[int] = 5 * 60  # 5 دقائق

# روابط الإنتاج المعتمدة
FRONTEND_URL: Final[str] = "https://75nn12345872168.github.io/agriculture/"
PROD_BACKEND_URL: Final[str] = "https://agriculture-qpvc.onrender.com"

CORS_ALLOWED_ORIGINS: Final[list] = [
    "https://75nn12345872168.github.io",
    "https://agriculture-qpvc.onrender.com",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://127.0.0.1:5500",
    "http://localhost:3000",
]

# Regex للسماح بأي نطاق فرعي على github.io (مطلوب لـ GitHub Pages)
CORS_ALLOWED_ORIGIN_REGEX: Final[str] = r"^https://[a-zA-Z0-9._-]+\.github\.io$"

GSCSI_CLASS_LABELS: Final[Dict[int, str]] = {
    1: "فشل",
    2: "خطر",
    3: "متوسط",
    4: "جيد",
    5: "ممتاز",
}
GSCSI_CLASS_PALETTE: Final[list] = ["a50026", "f46d43", "fee08b", "66bd63", "1a9850"]

# حدود التحقق
YEAR_MIN: Final[int] = 1990
YEAR_MAX: Final[int] = 2030

WEIGHT_VARIANTS: Final[Dict[str, Dict[str, float]]] = {
    "baseline": GSCSI_WEIGHTS,
    "dry_spell_boost": {
        "NDVI_Anomaly_Score": 0.14,
        "Rainfall_Anomaly_Score": 0.12,
        "VHI_Score": 0.13,
        "SPEI_Score": 0.08,
        "SoilMoisture_Score": 0.08,
        "LST_Score": 0.11,
        "NDMI_Score": 0.04,
        "LAI_Score": 0.05,
        "Dry_Spell_Score": 0.25,
    },
    "dry_spell_dominant": {
        "NDVI_Anomaly_Score": 0.10,
        "Rainfall_Anomaly_Score": 0.08,
        "VHI_Score": 0.10,
        "SPEI_Score": 0.07,
        "SoilMoisture_Score": 0.07,
        "LST_Score": 0.10,
        "NDMI_Score": 0.03,
        "LAI_Score": 0.10,
        "Dry_Spell_Score": 0.35,
    },
}

for _variant_name, _variant_weights in WEIGHT_VARIANTS.items():
    assert abs(sum(_variant_weights.values()) - 1.0) < 1e-9, (
        f"مجموع أوزان {_variant_name} يجب =1.0 وجد {sum(_variant_weights.values())}"
    )
