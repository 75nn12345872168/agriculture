# -*- coding: utf-8 -*-
"""
gscsi_engine.py
================
النسخة المُعاد هيكلتها من دفتر GSCSI (Gedaref Seasonal Crop Success Index) لتعمل
كوحدة خادوم (server-side) تُستدعى من Flask بدلاً من كونها خلايا Colab تفاعلية.

التحسينات الهندسية المطبّقة:
  1) تغليف جميع استدعاءات Earth Engine بـ tenacity مع exponential backoff عبر
     _gee_get_info و _gee_get_map_id.
  2) دمج استعلامات reduceRegion للملخص والمؤشرات الفرعية الـ 9 + الإحداثيات المركزية
     في استدعاء واحد (ee.Image.cat + ee.Dictionary) لتقليل زمن الاستجابة بشكل جذري.
  3) دمج استعلامات السلسلة الزمنية لـ NDVI في ee.Dictionary واحد مع فلترة أشهر الموسم.
  4) حماية التواريخ الحدّية في الموسم الجاري (YTD) لمنع إرسال نطاقات معكوسة إلى GEE.
  5) توحيد WEIGHT_VARIANTS من backend.config كمصدر حقيقة وحيد.
"""

import os
import json
import datetime
import functools
import threading
import logging
from typing import Dict, List, Optional, Tuple, Any

import ee
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

try:
    import backend.config as CFG
except ModuleNotFoundError:
    import config as CFG

logger = logging.getLogger("gscsi-engine")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


# ----------------------------------------------------------------------------
# 0) استدعاءات GEE محصّنة بـ tenacity ضد أخطاء الشبكة والحصة المؤقتة
# ----------------------------------------------------------------------------
def _is_retryable_gee_error(exc: BaseException) -> bool:
    """إعادة المحاولة للأخطاء الشبكية/المؤقتة فقط، وتجنّب إعادة أخطاء البرمجة أو المدخلات."""
    if isinstance(exc, (TypeError, ValueError, KeyError, AttributeError)):
        return False
    msg = str(exc).lower()
    non_retryable_tokens = (
        "invalid argument",
        "not found",
        "permission denied",
        "unauthenticated",
        "no valid",
    )
    if any(tok in msg for tok in non_retryable_tokens):
        return False
    return True


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_is_retryable_gee_error),
    reraise=True,
)
def _gee_get_info(ee_obj: Any) -> Any:
    """يجلب قيمة كائن Earth Engine إلى بايثون مع إعادة محاولة تلقائية عند أخطاء الشبكة المؤقتة."""
    return ee_obj.getInfo()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_is_retryable_gee_error),
    reraise=True,
)
def _gee_get_map_id(ee_image: Any, vis_params: dict) -> dict:
    """ينشئ معرف طبقة خرائط GEE مع إعادة محاولة تلقائية عند أخطاء الشبكة المؤقتة."""
    return ee_image.getMapId(vis_params)


# ----------------------------------------------------------------------------
# 1) المصادقة (Service Account) — تُقرأ من متغيّر بيئة GEE_SERVICE_ACCOUNT_JSON
#    (محتوى ملف JSON كاملاً كنص) أو من مسار ملف GEE_SERVICE_ACCOUNT_FILE.
# ----------------------------------------------------------------------------
_EE_LOCK = threading.Lock()
_EE_INITIALIZED = False


def init_earth_engine():
    global _EE_INITIALIZED
    if _EE_INITIALIZED:
        return
    with _EE_LOCK:
        if _EE_INITIALIZED:
            return
        key_json = os.environ.get("GEE_SERVICE_ACCOUNT_JSON")
        key_file = os.environ.get("GEE_SERVICE_ACCOUNT_FILE")

        if key_json:
            key_data = json.loads(key_json)
            client_email = key_data["client_email"]
            credentials = ee.ServiceAccountCredentials(client_email, key_data=key_json)
        elif key_file:
            with open(key_file, "r", encoding="utf-8") as f:
                key_data = json.load(f)
            client_email = key_data["client_email"]
            credentials = ee.ServiceAccountCredentials(client_email, key_file)
        else:
            raise RuntimeError(
                "لم يتم ضبط بيانات اعتماد GEE. عرّف GEE_SERVICE_ACCOUNT_JSON أو "
                "GEE_SERVICE_ACCOUNT_FILE في متغيرات البيئة."
            )

        project_id = key_data.get("project_id")
        ee.Initialize(credentials, project=project_id)
        _EE_INITIALIZED = True


def fc_to_df(fc) -> "pd.DataFrame":
    info = _gee_get_info(fc) or {}
    features = info.get("features", [])
    rows = [f.get("properties", {}) for f in features]
    return pd.DataFrame(rows)


def tile_url(ee_image, vis_params: dict) -> str:
    """يحوّل صورة GEE إلى رابط Tile (XYZ) يمكن لأي مكتبة خرائط ويب (Leaflet/Mapbox) استخدامه."""
    map_id = _gee_get_map_id(ee_image, vis_params)
    return map_id["tile_fetcher"].url_format


# ----------------------------------------------------------------------------
# 2) منطقة الدراسة (تُحسب مرة واحدة وتُخزَّن مؤقتاً)
# ----------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def load_cropland_mask():
    """
    قناع الأراضي الزراعية الفعلية — مصدره ESA WorldCover v200 (دقة 10م، 2021)، وهو
    مجاني ومتاح عالمياً في Earth Engine. الفئة 40 = أراضي زراعية (Cropland).
    """
    worldcover = ee.ImageCollection("ESA/WorldCover/v200").first()
    return worldcover.eq(40).rename("cropland")


@functools.lru_cache(maxsize=1)
def load_gedaref_boundaries():
    gaul1 = ee.FeatureCollection("FAO/GAUL/2015/level1")
    gaul2 = ee.FeatureCollection("FAO/GAUL/2015/level2")
    sudan = gaul1.filter(ee.Filter.eq("ADM0_NAME", "Sudan"))

    name_filter = ee.Filter.Or(
        ee.Filter.stringContains("ADM1_NAME", "Gadaref"),
        ee.Filter.stringContains("ADM1_NAME", "Gedaref"),
        ee.Filter.stringContains("ADM1_NAME", "Gedarif"),
        ee.Filter.stringContains("ADM1_NAME", "Qadarif"),
    )
    gedaref_state = sudan.filter(name_filter)
    if _gee_get_info(gedaref_state.size()) == 0:
        raise ValueError("تعذّر العثور على ولاية القضارف آلياً في طبقة GAUL.")

    adm1_name = gedaref_state.first().get("ADM1_NAME")
    localities = gaul2.filter(ee.Filter.eq("ADM1_NAME", adm1_name))
    return gedaref_state.geometry(), gedaref_state, localities


EPS = 1e-6


def _resolve_season_dates(
    season_year: int,
    start_month: int,
    end_month: int,
    today: Optional[datetime.date] = None,
) -> Tuple[datetime.date, datetime.date, bool]:
    """
    يتحقق من صلاحية نافذة الموسم مقارنةً بتاريخ اليوم لمنع إرسال نطاق زمني معكوس
    (SEASON_END <= SEASON_START) إلى Google Earth Engine.
    يعيد: (season_start_date, season_end_exclusive_date, is_ytd).
    """
    _today = today or datetime.date.today()
    season_start_py = datetime.date(season_year, start_month, 1)
    season_end_exclusive_py = (
        datetime.date(season_year + 1, 1, 1)
        if end_month == 12
        else datetime.date(season_year, end_month + 1, 1)
    )

    if season_year > _today.year or (season_year == _today.year and _today <= season_start_py):
        raise ValueError(
            f"موسم {season_year} (بداية من شهر {start_month}) لم يبدأ بعد حتى تاريخ اليوم "
            f"({_today.isoformat()}). يرجى اختيار موسم سابق أو نافذة زمنية متاحة."
        )

    is_ytd = (season_year == _today.year) and (_today < season_end_exclusive_py)
    effective_end_py = _today if is_ytd else season_end_exclusive_py
    if effective_end_py <= season_start_py:
        raise ValueError(
            f"نطاق الموسم غير صالح: تاريخ النهاية ({effective_end_py.isoformat()}) "
            f"يسبق أو يساوي تاريخ البداية ({season_start_py.isoformat()})."
        )
    return season_start_py, effective_end_py, is_ytd


# ----------------------------------------------------------------------------
# 3) خط الأنابيب الكامل — نفس منطق الدفتر، بشكل دالة واحدة قابلة للاستدعاء
# ----------------------------------------------------------------------------
def run_pipeline(season_year: int, start_month: int = 6, end_month: int = 10) -> Dict[str, Any]:
    _today = datetime.date.today()
    season_start_py, effective_end_py, is_ytd = _resolve_season_dates(
        season_year, start_month, end_month, today=_today
    )

    init_earth_engine()
    AOI, GEDAREF_STATE_FC, GEDAREF_LOCALITIES = load_gedaref_boundaries()
    CROPLAND_MASK = load_cropland_mask().clip(AOI)

    RAIN_HIST_START_YEAR = 1991
    RAIN_HIST_END_YEAR = season_year - 1
    MODIS_HIST_START_YEAR = 2000
    MODIS_HIST_END_YEAR = season_year - 1

    SEASON_START = ee.Date.fromYMD(season_year, start_month, 1)
    SEASON_END = ee.Date.fromYMD(season_year, end_month, 1).advance(1, "month")

    data_latency_adjusted = False
    if is_ytd:
        eff_end_month, eff_end_day = effective_end_py.month, effective_end_py.day
        SEASON_END = ee.Date.fromYMD(season_year, eff_end_month, eff_end_day)

        # ---- صمام أمان توفر البيانات (فجوة نشر CHIRPS) ----
        today_str = _today.isoformat()
        latest_chirps_img = (
            ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
            .filterBounds(AOI)
            .filterDate("2000-01-01", today_str)
            .sort("system:time_start", False)
            .first()
        )
        chirps_ts = latest_chirps_img.get("system:time_start")
        chirps_latest_str = _gee_get_info(
            ee.Algorithms.If(chirps_ts, ee.Date(chirps_ts).format("YYYY-MM-dd"), None)
        )
        if chirps_latest_str:
            chirps_latest_date = datetime.date.fromisoformat(chirps_latest_str)
            requested_end_date = datetime.date(season_year, eff_end_month, eff_end_day)
            if chirps_latest_date < requested_end_date:
                if chirps_latest_date <= season_start_py:
                    raise ValueError(
                        f"آخر بيانات أمطار CHIRPS المتاحة ({chirps_latest_date.isoformat()}) "
                        f"تسبق بداية موسم {season_year} ({season_start_py.isoformat()}). "
                        f"يرجى اختيار موسم سابق حتى تتوفر بيانات الموسم الجديد."
                    )
                eff_end_month, eff_end_day = chirps_latest_date.month, chirps_latest_date.day
                SEASON_END = ee.Date.fromYMD(season_year, eff_end_month, eff_end_day)
                data_latency_adjusted = True
    else:
        eff_end_month, eff_end_day = end_month, None

    def get_ytd_end_date(year):
        if is_ytd:
            return ee.Date.fromYMD(year, eff_end_month, eff_end_day)
        return ee.Date.fromYMD(year, end_month, 1).advance(1, "month")

    # ---------------- Sentinel-2 vegetation indices ----------------
    def mask_s2_clouds(image):
        scl = image.select("SCL")
        mask = (scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11)))
        return image.updateMask(mask).divide(10000).copyProperties(image, ["system:time_start"])

    def add_s2_indices(image):
        ndvi = image.normalizedDifference(["B8", "B4"]).rename("NDVI")
        ndmi = image.normalizedDifference(["B8", "B11"]).rename("NDMI")
        return image.addBands([ndvi, ndmi])

    def get_s2_season_composite(start_date, end_date, aoi, max_cloud=40):
        col = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(aoi)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud))
            .map(mask_s2_clouds)
            .map(add_s2_indices)
        )
        return col.select(["NDVI", "NDMI"]).median().clip(aoi)

    s2_current = get_s2_season_composite(SEASON_START, SEASON_END, AOI)

    # ---------------- Landsat LST + MODIS baseline ----------------
    def mask_landsat_clouds(image):
        qa = image.select("QA_PIXEL")
        cloud_mask = qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 4).eq(0))
        return image.updateMask(cloud_mask)

    def add_lst_landsat(image):
        lst = image.select("ST_B10").multiply(0.00341802).add(149.0).subtract(273.15).rename("LST")
        return image.addBands(lst)

    def get_landsat_lst_composite(start_date, end_date, aoi, max_cloud=40):
        l8 = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
        l9 = ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
        col = (
            l8.merge(l9)
            .filterBounds(aoi)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt("CLOUD_COVER", max_cloud))
            .map(mask_landsat_clouds)
            .map(add_lst_landsat)
        )
        return col.select("LST").median().clip(aoi)

    landsat_lst_current = get_landsat_lst_composite(SEASON_START, SEASON_END, AOI)

    MODIS_LST_COL = ee.ImageCollection("MODIS/061/MOD11A2").select("LST_Day_1km")

    def modis_seasonal_mean(collection, year, s_month, e_month, aoi, scale_factor, offset=0.0):
        start = ee.Date.fromYMD(year, s_month, 1)
        end = get_ytd_end_date(year)
        return collection.filterBounds(aoi).filterDate(start, end).mean().multiply(scale_factor).add(offset).clip(aoi)

    def modis_historical_collection(collection, hist_start, hist_end, s_month, e_month, aoi, scale_factor, offset=0.0):
        years = ee.List.sequence(hist_start, hist_end)

        def yearly(y):
            y = ee.Number(y)
            start = ee.Date.fromYMD(y, s_month, 1)
            end = get_ytd_end_date(y) if is_ytd else ee.Date.fromYMD(y, e_month, 1).advance(1, "month")
            img = collection.filterBounds(aoi).filterDate(start, end).mean().multiply(scale_factor).add(offset)
            return img.set("year", y).clip(aoi)

        return ee.ImageCollection.fromImages(years.map(yearly))

    lst_current_modis = modis_seasonal_mean(MODIS_LST_COL, season_year, start_month, end_month, AOI, 0.02, -273.15)
    lst_hist_col = modis_historical_collection(
        MODIS_LST_COL, MODIS_HIST_START_YEAR, MODIS_HIST_END_YEAR, start_month, end_month, AOI, 0.02, -273.15
    )
    lst_hist_mean = lst_hist_col.mean().clip(AOI)
    lst_hist_min = lst_hist_col.min().clip(AOI)
    lst_hist_max = lst_hist_col.max().clip(AOI)

    sensor_offset = ee.Number(
        landsat_lst_current.subtract(lst_current_modis)
        .reduceRegion(reducer=ee.Reducer.mean(), geometry=AOI, scale=500, maxPixels=1e9, tileScale=4)
        .get("LST")
    )
    lst_hist_mean_adj = lst_hist_mean.add(sensor_offset).rename("LST_mean_hist_adj")
    lst_anomaly = landsat_lst_current.subtract(lst_hist_mean_adj).rename("LST_Anomaly")

    # ---------------- CHIRPS rainfall ----------------
    CHIRPS = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").select("precipitation")

    def seasonal_rainfall(year, s_month, e_month, aoi):
        start = ee.Date.fromYMD(year, s_month, 1)
        end = get_ytd_end_date(year)
        return CHIRPS.filterBounds(aoi).filterDate(start, end).sum().clip(aoi)

    def historical_seasonal_collection(hist_start, hist_end, s_month, e_month, aoi):
        years = ee.List.sequence(hist_start, hist_end)

        def yearly_sum(y):
            y = ee.Number(y)
            start = ee.Date.fromYMD(y, s_month, 1)
            end = get_ytd_end_date(y)
            return CHIRPS.filterBounds(aoi).filterDate(start, end).sum().set("year", y).clip(aoi)

        return ee.ImageCollection.fromImages(years.map(yearly_sum))

    rain_current_season = seasonal_rainfall(season_year, start_month, end_month, AOI)
    rain_hist_col = historical_seasonal_collection(RAIN_HIST_START_YEAR, RAIN_HIST_END_YEAR, start_month, end_month, AOI)
    rain_hist_mean = rain_hist_col.mean().clip(AOI)
    rain_hist_std = rain_hist_col.reduce(ee.Reducer.stdDev()).rename("precipitation_stdDev").clip(AOI)

    rainfall_anomaly_pct = (
        rain_current_season.subtract(rain_hist_mean).divide(rain_hist_mean.abs().add(EPS)).multiply(100)
        .rename("Rainfall_Anomaly_Pct")
    )
    SPI = rain_current_season.subtract(rain_hist_mean).divide(rain_hist_std.add(EPS)).rename("SPI")

    # ---------------- كشف بداية الموسم الفعلية (Rainy Season Onset) ----------------
    ONSET_SEARCH_START_MONTH = CFG.ONSET["search_start_month"]
    ONSET_SEARCH_N_DEKADS = CFG.ONSET["n_dekads"]
    ONSET_MIN_DEKAD_MM = CFG.ONSET["min_mm"]
    ONSET_CONFIRM_DEKADS = CFG.ONSET["confirm_dekads"]
    ONSET_CONFIRM_MIN_MM = CFG.ONSET["confirm_min_mm"]

    def get_dekad_sums_for_year(year, aoi):
        start = ee.Date.fromYMD(year, ONSET_SEARCH_START_MONTH, 1)
        source = CHIRPS

        if is_ytd and year == season_year:
            cutoff = ee.Date.fromYMD(season_year, eff_end_month, eff_end_day)
            n_days_avail = cutoff.difference(start, "day").max(0)
            n_dekads = ee.Number(n_days_avail).divide(10).floor().min(ONSET_SEARCH_N_DEKADS)
        else:
            n_dekads = ee.Number(ONSET_SEARCH_N_DEKADS)
        dekad_list = ee.List.sequence(0, n_dekads.subtract(1))

        def one(i):
            i = ee.Number(i)
            d_start = start.advance(i.multiply(10), "day")
            d_end = d_start.advance(10, "day")
            val = (
                source.filterBounds(aoi).filterDate(d_start, d_end).sum()
                .reduceRegion(reducer=ee.Reducer.mean(), geometry=aoi, scale=5000, maxPixels=1e9)
                .get("precipitation", 0)
            )
            return ee.Algorithms.If(val, val, 0)

        return dekad_list.map(one)

    def find_onset_dekad_index(dekad_sums):
        """منطق كشف بداية الموسم يُنفَّذ في بايثون بعد سحب مجاميع العشاريات دفعة واحدة."""
        if not dekad_sums:
            return None
        n = len(dekad_sums)
        for i in range(n - ONSET_CONFIRM_DEKADS):
            if dekad_sums[i] >= ONSET_MIN_DEKAD_MM:
                following = sum(dekad_sums[i + 1: i + 1 + ONSET_CONFIRM_DEKADS])
                if following >= ONSET_CONFIRM_MIN_MM:
                    return i
        return None

    onset_years = list(range(RAIN_HIST_START_YEAR, season_year + 1))
    onset_years_ee = ee.List(onset_years)
    onset_matrix_ee = onset_years_ee.map(lambda y: get_dekad_sums_for_year(ee.Number(y), AOI))
    onset_matrix = _gee_get_info(onset_matrix_ee) or []

    onset_indices = {y: find_onset_dekad_index(sums) for y, sums in zip(onset_years, onset_matrix)}
    current_onset_idx = onset_indices.get(season_year)
    hist_onset_idxs = [v for k, v in onset_indices.items() if k != season_year and v is not None]
    mean_hist_onset_idx = round(sum(hist_onset_idxs) / len(hist_onset_idxs)) if hist_onset_idxs else None

    def dekad_idx_to_date(idx, year):
        base = datetime.date(year, ONSET_SEARCH_START_MONTH, 1)
        return base + datetime.timedelta(days=idx * 10)

    onset_date_current = dekad_idx_to_date(current_onset_idx, season_year) if current_onset_idx is not None else None
    onset_date_typical = (
        dekad_idx_to_date(mean_hist_onset_idx, season_year) if mean_hist_onset_idx is not None else None
    )
    onset_anomaly_days = (
        (onset_date_current - onset_date_typical).days
        if (onset_date_current and onset_date_typical) else None
    )

    # ---------------- نافذة الإزهار (FAO-56 للذرة الرفيعة) ----------------
    FLOWERING_START_OFFSET_DAYS = CFG.FLOWERING["start_offset_days"]
    FLOWERING_END_OFFSET_DAYS = CFG.FLOWERING["end_offset_days"]

    flowering_window = None
    flowering_rain_anomaly_pct = None
    if onset_date_current is not None:
        flow_start = onset_date_current + datetime.timedelta(days=FLOWERING_START_OFFSET_DAYS)
        flow_end = onset_date_current + datetime.timedelta(days=FLOWERING_END_OFFSET_DAYS)
        data_cutoff = (
            datetime.date(season_year, eff_end_month, eff_end_day) if is_ytd
            else datetime.date(season_year, end_month, 28)
        )
        if flow_start <= data_cutoff and (min(flow_end, data_cutoff) - flow_start).days >= 3:
            flow_end_clamped = min(flow_end, data_cutoff)
            flow_start_ee = ee.Date.fromYMD(flow_start.year, flow_start.month, flow_start.day)
            flow_end_ee = ee.Date.fromYMD(flow_end_clamped.year, flow_end_clamped.month, flow_end_clamped.day)
            flowering_current_source = CHIRPS

            flowering_rain_current = (
                flowering_current_source.filterBounds(AOI).filterDate(flow_start_ee, flow_end_ee).sum()
                .reduceRegion(reducer=ee.Reducer.mean(), geometry=AOI, scale=5000, maxPixels=1e9)
                .get("precipitation", 0)
            )

            def hist_flowering_sum(y):
                y = ee.Number(y)
                s = ee.Date.fromYMD(y, flow_start.month, flow_start.day)
                e = ee.Date.fromYMD(y, flow_end_clamped.month, flow_end_clamped.day)
                return CHIRPS.filterBounds(AOI).filterDate(s, e).sum().set("year", y)

            hist_years_list = ee.List.sequence(RAIN_HIST_START_YEAR, RAIN_HIST_END_YEAR)
            hist_flowering_col = ee.ImageCollection.fromImages(hist_years_list.map(hist_flowering_sum))
            flowering_rain_hist_mean = (
                hist_flowering_col.mean()
                .reduceRegion(reducer=ee.Reducer.mean(), geometry=AOI, scale=5000, maxPixels=1e9)
                .get("precipitation", 0)
            )

            vals = _gee_get_info(ee.Dictionary({
                "current": flowering_rain_current,
                "hist": flowering_rain_hist_mean,
            })) or {}
            cur_v, hist_v = vals.get("current"), vals.get("hist")
            if cur_v is not None and hist_v not in (None, 0):
                flowering_rain_anomaly_pct = round((cur_v - hist_v) / abs(hist_v) * 100, 1)
            flowering_window = [flow_start.isoformat(), flow_end.isoformat()]

    # ---------------- MODIS LAI ----------------
    def get_modis_lai(start_date, end_date, aoi):
        col = ee.ImageCollection("MODIS/061/MOD15A2H").filterBounds(aoi).filterDate(start_date, end_date)

        def scale_and_mask(img):
            lai = img.select("Lai_500m").multiply(0.1).rename("LAI")
            qc = img.select("FparLai_QC")
            good_quality = qc.bitwiseAnd(1).eq(0)
            return img.addBands([lai]).updateMask(good_quality)

        return col.map(scale_and_mask).select(["LAI"]).median().clip(aoi)

    modis_lai_current = get_modis_lai(SEASON_START, SEASON_END, AOI)

    # ---------------- ERA5-Land soil moisture ----------------
    def get_era5_soil(start_date, end_date, aoi):
        col = ee.ImageCollection("ECMWF/ERA5_LAND/MONTHLY_AGGR").filterBounds(aoi).filterDate(start_date, end_date)
        return col.select("volumetric_soil_water_layer_1").mean().rename("Soil_Moisture").clip(aoi)

    era5_current = get_era5_soil(SEASON_START, SEASON_END, AOI)

    # ---------------- VCI / TCI / VHI ----------------
    MODIS_NDVI_COL = ee.ImageCollection("MODIS/061/MOD13Q1").select("NDVI")
    ndvi_current_modis = modis_seasonal_mean(MODIS_NDVI_COL, season_year, start_month, end_month, AOI, 0.0001)
    ndvi_hist_col = modis_historical_collection(
        MODIS_NDVI_COL, MODIS_HIST_START_YEAR, MODIS_HIST_END_YEAR, start_month, end_month, AOI, 0.0001
    )
    ndvi_hist_mean = ndvi_hist_col.mean().clip(AOI)
    ndvi_hist_min = ndvi_hist_col.min().clip(AOI)
    ndvi_hist_max = ndvi_hist_col.max().clip(AOI)

    ndvi_anomaly_pct = (
        ndvi_current_modis.subtract(ndvi_hist_mean).divide(ndvi_hist_mean.abs().add(EPS)).multiply(100)
        .rename("NDVI_Anomaly_Pct")
    )
    VCI = (
        ndvi_current_modis.subtract(ndvi_hist_min).divide(ndvi_hist_max.subtract(ndvi_hist_min).add(EPS)).multiply(100)
        .rename("VCI").clamp(0, 100)
    )
    TCI = (
        lst_hist_max.subtract(lst_current_modis).divide(lst_hist_max.subtract(lst_hist_min).add(EPS)).multiply(100)
        .rename("TCI").clamp(0, 100)
    )
    VHI = VCI.multiply(0.5).add(TCI.multiply(0.5)).rename("VHI")

    # ---------------- Dekadal dry-spell score (spatial) ----------------
    DRY_DEKAD_THRESHOLD_MM = CFG.DRY_DEKAD_THRESHOLD_MM

    def get_dekadal_rainfall_collection(year, s_month, e_month, aoi):
        start = ee.Date.fromYMD(year, s_month, 1)
        if is_ytd and year == season_year:
            end = ee.Date.fromYMD(year, eff_end_month, eff_end_day)
        else:
            end = ee.Date.fromYMD(year, e_month, 1).advance(1, "month")
        n_days = end.difference(start, "day")
        n_dekads = ee.Number(n_days).divide(10).floor().max(0)
        dekad_list = ee.List.sequence(0, n_dekads.subtract(1))

        def dekad_sum(i):
            i = ee.Number(i)
            d_start = start.advance(i.multiply(10), "day")
            d_end = d_start.advance(10, "day")
            img = CHIRPS.filterBounds(aoi).filterDate(d_start, d_end).sum().clip(aoi)
            return img.set("dekad_index", i)

        return ee.ImageCollection.fromImages(dekad_list.map(dekad_sum))

    dekadal_col_current = get_dekadal_rainfall_collection(season_year, 5, 11, AOI)
    dry_dekad_mask = dekadal_col_current.map(lambda img: img.lt(DRY_DEKAD_THRESHOLD_MM).rename("dry"))
    dry_dekad_count = dry_dekad_mask.sum().rename("Dry_Dekads")
    n_dekads_total = ee.Number(dekadal_col_current.size())
    dry_spell_score = (
        ee.Image(100).subtract(dry_dekad_count.divide(n_dekads_total.max(1)).multiply(100))
        .rename("Dry_Spell_Score").clip(AOI)
    )

    # ---------------- Composite GSCSI ----------------
    GSCSI_WEIGHTS = CFG.GSCSI_WEIGHTS

    def normalize_band(image, band, vmin, vmax, invert=False, out_name=None):
        img = image.select(band)
        norm = img.subtract(vmin).divide(vmax - vmin).multiply(100).clamp(0, 100)
        if invert:
            norm = ee.Image(100).subtract(norm)
        return norm.rename(out_name or (band + "_Score"))

    NDVI_Anomaly_Score = normalize_band(ndvi_anomaly_pct, "NDVI_Anomaly_Pct", -40, 40, out_name="NDVI_Anomaly_Score")
    Rainfall_Anomaly_Score = normalize_band(rainfall_anomaly_pct, "Rainfall_Anomaly_Pct", -60, 60, out_name="Rainfall_Anomaly_Score")
    VHI_Score = VHI.rename("VHI_Score")
    SPEI_Score = normalize_band(SPI, "SPI", -2.5, 2.5, out_name="SPEI_Score")
    SoilMoisture_Score = normalize_band(era5_current, "Soil_Moisture", 0.05, 0.35, out_name="SoilMoisture_Score")
    LST_Score = normalize_band(lst_anomaly, "LST_Anomaly", -3, 3, invert=True, out_name="LST_Score")
    NDMI_Score = normalize_band(s2_current, "NDMI", -0.3, 0.6, out_name="NDMI_Score")
    LAI_Score = normalize_band(modis_lai_current, "LAI", 0, 5, out_name="LAI_Score")
    DrySpell_Score = dry_spell_score.rename("Dry_Spell_Score")

    score_bands = ee.Image.cat(
        [NDVI_Anomaly_Score, Rainfall_Anomaly_Score, VHI_Score, SPEI_Score,
         SoilMoisture_Score, LST_Score, NDMI_Score, LAI_Score, DrySpell_Score]
    )

    GSCSI = ee.Image(0)
    for band_name, weight in GSCSI_WEIGHTS.items():
        GSCSI = GSCSI.add(score_bands.select(band_name).multiply(weight))
    GSCSI = GSCSI.rename("GSCSI").clip(AOI)

    GSCSI_CLASS_LABELS = CFG.GSCSI_CLASS_LABELS
    GSCSI_CLASS_PALETTE = CFG.GSCSI_CLASS_PALETTE

    GSCSI_CLASS = (
        ee.Image(1)
        .where(GSCSI.gte(25), 2).where(GSCSI.gte(50), 3).where(GSCSI.gte(75), 4).where(GSCSI.gte(90), 5)
        .rename("GSCSI_Class").updateMask(GSCSI.mask())
    )

    # ---------------- Tile URLs for the web map ----------------
    gscsi_vis = {"min": 0, "max": 100, "palette": GSCSI_CLASS_PALETTE}
    class_vis = {"min": 1, "max": 5, "palette": GSCSI_CLASS_PALETTE}
    ndvi_vis = {"min": -0.2, "max": 0.9, "palette": ["a50026", "fee08b", "1a9850"]}
    rain_anom_vis = {"min": -60, "max": 60, "palette": ["a50026", "ffffbf", "1a9850"]}

    tiles = {
        "gscsi": tile_url(GSCSI.updateMask(CROPLAND_MASK), gscsi_vis),
        "class": tile_url(GSCSI_CLASS.updateMask(CROPLAND_MASK), class_vis),
        "ndvi": tile_url(s2_current.select("NDVI").updateMask(CROPLAND_MASK), ndvi_vis),
        "rainfall_anomaly": tile_url(rainfall_anomaly_pct.updateMask(CROPLAND_MASK), rain_anom_vis),
    }

    # ---------------- Zonal stats per locality ----------------
    zonal_image = (
        GSCSI.addBands(score_bands).addBands(GSCSI_CLASS)
        .addBands(rain_current_season.rename("Rain_Season_mm"))
        .addBands(rainfall_anomaly_pct)
        .addBands(s2_current)
        .addBands(VCI.rename("VCI")).addBands(TCI.rename("TCI"))
    )

    def zonal_stats_df(image):
        fc = image.reduceRegions(collection=GEDAREF_LOCALITIES, reducer=ee.Reducer.mean(), scale=500, tileScale=8)
        df = fc_to_df(fc)
        if "ADM2_NAME" in df.columns:
            return df.set_index("ADM2_NAME")
        return df

    # نحسب نسخة الأراضي الزراعية أولاً، ولا نلجأ للنسخة الكاملة إلا عند وجود محلية ناقصة فعلياً
    df_cropland = zonal_stats_df(zonal_image.updateMask(CROPLAND_MASK))
    crop_mask_coverage = (
        int(df_cropland["GSCSI"].notna().sum()) if "GSCSI" in df_cropland.columns else 0
    )
    needs_zonal_fallback = (
        df_cropland.empty
        or "GSCSI" not in df_cropland.columns
        or bool(df_cropland["GSCSI"].isna().any())
    )
    if needs_zonal_fallback:
        df_fallback = zonal_stats_df(zonal_image)
        df_stats = df_cropland.combine_first(df_fallback).reset_index()
    else:
        df_stats = df_cropland.reset_index()

    if "GSCSI_Class" in df_stats.columns:
        df_stats["GSCSI_Class_Label"] = df_stats["GSCSI_Class"].round().map(GSCSI_CLASS_LABELS)
    keep_cols = [c for c in [
        "ADM2_NAME", "GSCSI", "GSCSI_Class_Label", "Rain_Season_mm", "Rainfall_Anomaly_Pct",
        "NDVI", "NDMI", "VCI", "TCI",
    ] if c in df_stats.columns]
    if "GSCSI" in df_stats.columns:
        df_stats = df_stats[keep_cols].sort_values("GSCSI", ascending=False).reset_index(drop=True)
    else:
        df_stats = df_stats[keep_cols].reset_index(drop=True)
    localities = json.loads(df_stats.round(2).to_json(orient="records", force_ascii=False))

    # ---------------- NDVI current vs. historical climatology chart (Batched) ----------------
    def add_mean_ndvi(img):
        mean_val = img.reduceRegion(reducer=ee.Reducer.mean(), geometry=AOI, scale=500, maxPixels=1e9, tileScale=4).get("NDVI")
        return img.set("ndvi_mean", mean_val, "date_str", img.date().format("YYYY-MM-dd"))

    current_season_ndvi_col = MODIS_NDVI_COL.filterBounds(AOI).filterDate(SEASON_START, SEASON_END).map(add_mean_ndvi)
    hist_ndvi_col = (
        MODIS_NDVI_COL.filterBounds(AOI)
        .filterDate(
            ee.Date.fromYMD(MODIS_HIST_START_YEAR, start_month, 1),
            ee.Date.fromYMD(MODIS_HIST_END_YEAR, end_month, 1).advance(1, "month"),
        )
        .filter(ee.Filter.calendarRange(start_month, end_month, "month"))
        .map(add_mean_ndvi)
    )

    ndvi_payload = _gee_get_info(ee.Dictionary({
        "current_dates": current_season_ndvi_col.aggregate_array("date_str"),
        "current_vals": current_season_ndvi_col.aggregate_array("ndvi_mean"),
        "hist_dates": hist_ndvi_col.aggregate_array("date_str"),
        "hist_vals": hist_ndvi_col.aggregate_array("ndvi_mean"),
    })) or {}

    current_dates = ndvi_payload.get("current_dates") or []
    current_vals = ndvi_payload.get("current_vals") or []
    hist_dates = ndvi_payload.get("hist_dates") or []
    hist_vals = ndvi_payload.get("hist_vals") or []

    if current_dates and current_vals:
        df_current = pd.DataFrame({"date": pd.to_datetime(current_dates), "ndvi": current_vals})
        df_current["ndvi"] = pd.to_numeric(df_current["ndvi"], errors="coerce") * 0.0001
        df_current["doy"] = df_current["date"].dt.day_of_year
        current_records = json.loads(df_current[["doy", "ndvi"]].dropna().round(4).to_json(orient="records"))
    else:
        current_records = []

    if hist_dates and hist_vals:
        df_hist = pd.DataFrame({"date": pd.to_datetime(hist_dates), "ndvi": hist_vals})
        df_hist["ndvi"] = pd.to_numeric(df_hist["ndvi"], errors="coerce") * 0.0001
        df_hist["doy"] = df_hist["date"].dt.day_of_year
        hist_climatology = df_hist.dropna().groupby("doy")["ndvi"].agg(["mean", "std"]).reset_index()
        hist_records = json.loads(hist_climatology.round(4).to_json(orient="records"))
    else:
        hist_records = []

    ndvi_timeseries = {
        "current": current_records,
        "historical": hist_records,
    }

    # ---------------- Summary (Single Batched Multi-Band reduceRegion + Centroid) ----------------
    # دمج جميع المؤشرات (GSCSI + المكونات الـ 9 + الأمطار + انحراف الأمطار) في صورة واحدة
    # واستدعاء reduceRegion مرة واحدة فقط بدلاً من 12-24 استدعاء متسلسل.
    summary_image = ee.Image.cat([
        GSCSI,
        score_bands,
        rain_current_season.rename("precipitation"),
        rainfall_anomaly_pct,
    ])
    masked_summary_ee = summary_image.updateMask(CROPLAND_MASK).reduceRegion(
        reducer=ee.Reducer.mean(), geometry=AOI, scale=1000, maxPixels=1e9, tileScale=4
    )
    summary_payload = _gee_get_info(ee.Dictionary({
        "masked": masked_summary_ee,
        "centroid": AOI.centroid(1).coordinates(),
    })) or {}

    masked_vals = summary_payload.get("masked") or {}
    centroid = summary_payload.get("centroid") or [35.38, 14.03]

    needed_bands = ["GSCSI", *GSCSI_WEIGHTS.keys(), "precipitation", "Rainfall_Anomaly_Pct"]
    missing_bands = [b for b in needed_bands if masked_vals.get(b) is None]
    fallback_vals = {}
    if missing_bands:
        fallback_vals = _gee_get_info(
            summary_image.select(missing_bands).reduceRegion(
                reducer=ee.Reducer.mean(), geometry=AOI, scale=1000, maxPixels=1e9, tileScale=4
            )
        ) or {}

    def _pick_summary(band_name: str):
        v = masked_vals.get(band_name)
        return v if v is not None else fallback_vals.get(band_name)

    gscsi_mean_val = _pick_summary("GSCSI")
    gscsi_mean = round(gscsi_mean_val, 1) if gscsi_mean_val is not None else None

    component_scores = {}
    for band_name in GSCSI_WEIGHTS.keys():
        val = _pick_summary(band_name)
        component_scores[band_name] = round(val, 2) if val is not None else None

    if gscsi_mean is None:
        gscsi_class_label = "غير متوفر"
    elif gscsi_mean >= 90:
        gscsi_class_label = "ممتاز"
    elif gscsi_mean >= 75:
        gscsi_class_label = "جيد"
    elif gscsi_mean >= 50:
        gscsi_class_label = "متوسط"
    elif gscsi_mean >= 25:
        gscsi_class_label = "خطر"
    else:
        gscsi_class_label = "فشل"

    rain_mean_val = _pick_summary("precipitation")
    rain_anom_val = _pick_summary("Rainfall_Anomaly_Pct")

    return {
        "meta": {
            "season_year": season_year,
            "start_month": start_month,
            "end_month": end_month,
            "is_ytd": is_ytd,
            "effective_cutoff_date": (
                f"{season_year:04d}-{eff_end_month:02d}-{eff_end_day:02d}" if is_ytd else None
            ),
            "data_latency_adjusted": data_latency_adjusted,
            "crop_mask_applied": True,
            "crop_mask_source": "ESA WorldCover v200 (2021)",
            "localities_with_cropland_data": crop_mask_coverage,
            "generated_at": _utc_now_iso(),
        },
        "summary": {
            "gscsi_mean": gscsi_mean,
            "gscsi_class_label": gscsi_class_label,
            "rain_season_mm": round(rain_mean_val, 1) if rain_mean_val is not None else None,
            "rainfall_anomaly_pct": round(rain_anom_val, 1) if rain_anom_val is not None else None,
            "component_scores": component_scores,
        },
        "map": {
            "center": [centroid[1], centroid[0]],
            "zoom": 8,
            "tiles": tiles,
            "class_labels": GSCSI_CLASS_LABELS,
            "class_palette": GSCSI_CLASS_PALETTE,
        },
        "localities": localities,
        "ndvi_timeseries": ndvi_timeseries,
        "phenology": {
            "onset_date": onset_date_current.isoformat() if onset_date_current else None,
            "typical_onset_date": onset_date_typical.isoformat() if onset_date_typical else None,
            "onset_anomaly_days": onset_anomaly_days,
            "flowering_window": flowering_window,
            "flowering_rainfall_anomaly_pct": flowering_rain_anomaly_pct,
            "data_cutoff_date": (
                f"{season_year:04d}-{eff_end_month:02d}-{eff_end_day:02d}" if is_ytd else None
            ),
            "note": (
                "تاريخ بداية الموسم مُكتشف ديناميكياً من بيانات الأمطار الفعلية "
                "(3 عشاريات متتالية بأمطار كافية)، ونافذة الإزهار مُشتقة منه حسب "
                "أطوال مراحل نمو الذرة الرفيعة القياسية (FAO-56). البيانات من CHIRPS "
                "النهائي (الأدق والمُتحقَّق من مطابقته للقيم المرجعية)، فقد تظهر بداية "
                "الموسم متأخرة عن الواقع الفعلي بسبب فجوة نشر البيانات المعتادة."
            ),
        },
    }


# ==========================================================================
# Leave-One-Year-Out (LOYO) Validation
# ==========================================================================
def _ground_truth_path():
    return os.path.join(os.path.dirname(__file__), "data", "gedaref_ground_truth_yields.json")


def _load_ground_truth(crop: str, sector: str):
    path = _ground_truth_path()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    records = [
        r for r in data.get("records", [])
        if r.get("crop") == crop
        and r.get("sector") == sector
        and r.get("yield_t_ha") is not None
    ]
    records.sort(key=lambda r: r["season_year"])
    return records


def _fit_linear(xs, ys):
    """انحدار خطي بسيط (least squares) بدون مكتبات خارجية إضافية."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        slope = 0.0
    else:
        slope = cov / var_x
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _pearson_r(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = (var_x * var_y) ** 0.5
    if denom == 0:
        return None
    return cov / denom


def run_loyo_validation(crop: str = "sorghum", sector: str = "semi_mechanized",
                         start_month: int = 6, end_month: int = 10):
    ground_truth = _load_ground_truth(crop, sector)
    n = len(ground_truth)

    MIN_YEARS_FOR_MEANINGFUL_LOYO = 6

    if n < 3:
        return {
            "error": "بيانات غير كافية لإجراء LOYO",
            "crop": crop,
            "sector": sector,
            "n_years_available": n,
            "min_years_required": 3,
            "note": "أضف سنوات إضافية إلى gedaref_ground_truth_yields.json",
        }

    points = []
    for record in ground_truth:
        year = record["season_year"]
        pipeline_result = run_pipeline(year, start_month, end_month)
        gscsi_mean = pipeline_result["summary"]["gscsi_mean"]
        if gscsi_mean is None:
            continue
        points.append({
            "season_year": year,
            "gscsi_mean": gscsi_mean,
            "actual_yield_t_ha": record["yield_t_ha"],
            "source_report": record.get("source_report"),
        })

    n_valid = len(points)
    if n_valid < 3:
        return {
            "error": "تعذر حساب GSCSI لعدد كافٍ من السنوات (فشل استعلامات GEE على الأرجح)",
            "crop": crop,
            "sector": sector,
            "n_years_available": n,
            "n_years_with_valid_gscsi": n_valid,
        }

    xs_all = [p["gscsi_mean"] for p in points]
    ys_all = [p["actual_yield_t_ha"] for p in points]

    loyo_results = []
    for i in range(n_valid):
        train_points = points[:i] + points[i + 1:]
        test_point = points[i]
        xs_train = [p["gscsi_mean"] for p in train_points]
        ys_train = [p["actual_yield_t_ha"] for p in train_points]
        slope, intercept = _fit_linear(xs_train, ys_train)
        predicted = slope * test_point["gscsi_mean"] + intercept
        error = predicted - test_point["actual_yield_t_ha"]
        loyo_results.append({
            "season_year": test_point["season_year"],
            "gscsi_mean": test_point["gscsi_mean"],
            "actual_yield_t_ha": test_point["actual_yield_t_ha"],
            "predicted_yield_t_ha": round(predicted, 3),
            "error_t_ha": round(error, 3),
        })

    errors = [r["error_t_ha"] for r in loyo_results]
    mae = sum(abs(e) for e in errors) / len(errors)
    rmse = (sum(e ** 2 for e in errors) / len(errors)) ** 0.5

    actual_mean = sum(ys_all) / len(ys_all)
    ss_tot = sum((y - actual_mean) ** 2 for y in ys_all)
    ss_res = sum(e ** 2 for e in errors)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else None

    pearson_full_sample = _pearson_r(xs_all, ys_all)

    warnings = []
    if n_valid < MIN_YEARS_FOR_MEANINGFUL_LOYO:
        warnings.append(
            f"عدد السنوات المتاحة ({n_valid}) أقل من الحد الأدنى المقترح "
            f"({MIN_YEARS_FOR_MEANINGFUL_LOYO}) لتحقق إحصائي موثوق. اعتبر هذه "
            "النتائج مؤشراً استكشافياً أولياً فقط، وليست تأكيداً نهائياً لدقة النموذج."
        )

    return {
        "crop": crop,
        "sector": sector,
        "n_years_available": n,
        "n_years_used": n_valid,
        "min_years_for_reliable_loyo": MIN_YEARS_FOR_MEANINGFUL_LOYO,
        "per_year_results": loyo_results,
        "metrics": {
            "mae_t_ha": round(mae, 3),
            "rmse_t_ha": round(rmse, 3),
            "r_squared_loyo": round(r_squared, 3) if r_squared is not None else None,
            "pearson_r_full_sample": round(pearson_full_sample, 3) if pearson_full_sample is not None else None,
        },
        "warnings": warnings,
        "generated_at": _utc_now_iso(),
    }


# ==========================================================================
# تحليل حساسية الأوزان (Weight Sensitivity Analysis) — مصدر الحقيقة في config.py
# ==========================================================================
WEIGHT_VARIANTS = CFG.WEIGHT_VARIANTS


def run_weight_sensitivity_analysis(crop: str = "sorghum", sector: str = "semi_mechanized",
                                     start_month: int = 6, end_month: int = 10):
    ground_truth = _load_ground_truth(crop, sector)
    n = len(ground_truth)
    if n < 3:
        return {"error": "بيانات غير كافية", "n_years_available": n}

    per_year_components = []
    for record in ground_truth:
        year = record["season_year"]
        pipeline_result = run_pipeline(year, start_month, end_month)
        components = pipeline_result["summary"].get("component_scores", {})
        if not components or any(v is None for v in components.values()):
            continue
        per_year_components.append({
            "season_year": year,
            "actual_yield_t_ha": record["yield_t_ha"],
            "components": components,
        })

    n_valid = len(per_year_components)
    if n_valid < 3:
        return {"error": "تعذر حساب المؤشرات الفرعية لعدد كافٍ من السنوات",
                "n_years_available": n, "n_years_with_valid_components": n_valid}

    variant_results = {}
    for variant_name, weights in WEIGHT_VARIANTS.items():
        gscsi_values, yield_values, per_year = [], [], []
        for entry in per_year_components:
            weighted_gscsi = sum(
                entry["components"].get(band, 0) * w for band, w in weights.items()
            )
            gscsi_values.append(weighted_gscsi)
            yield_values.append(entry["actual_yield_t_ha"])
            per_year.append({
                "season_year": entry["season_year"],
                "weighted_gscsi": round(weighted_gscsi, 2),
                "actual_yield_t_ha": entry["actual_yield_t_ha"],
            })
        r = _pearson_r(gscsi_values, yield_values)
        variant_results[variant_name] = {
            "weights": weights,
            "pearson_r": round(r, 3) if r is not None else None,
            "per_year": per_year,
        }

    return {
        "crop": crop,
        "sector": sector,
        "n_years_used": n_valid,
        "min_years_for_reliable_analysis": 6,
        "warning": (
            None if n_valid >= 6 else
            f"عدد السنوات ({n_valid}) أقل من الحد الأدنى الموصى به (6) — "
            "هذا تحليل استكشافي أولي فقط."
        ),
        "variants_tested_predefined_before_seeing_results": list(WEIGHT_VARIANTS.keys()),
        "results": variant_results,
        "generated_at": _utc_now_iso(),
    }
