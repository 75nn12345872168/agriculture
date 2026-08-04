# -*- coding: utf-8 -*-
"""
gscsi_engine.py
================
النسخة المُعاد هيكلتها من دفتر GSCSI (Gedaref Seasonal Crop Success Index) لتعمل
كوحدة خادوم (server-side) تُستدعى من Flask بدلاً من كونها خلايا Colab تفاعلية.

المنطق العلمي والحسابي **لم يتغيّر** عن الدفتر الأصلي — فقط:
  1) المصادقة أصبحت عبر Service Account من متغيّر بيئة بدل رفع ملف يدوياً في Colab.
  2) نتائج الخرائط تُعاد كروابط Tile (XYZ) عبر ee.Image.getMapId() بدل عرضها في geemap.
  3) لا يوجد matplotlib/rasterio/تصدير محلي — النتائج ترجع كـ JSON للواجهة الأمامية.
"""

import os
import json
import datetime
import functools
import threading

import ee
import pandas as pd

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


def fc_to_df(fc):
    features = fc.getInfo()["features"]
    rows = [f.get("properties", {}) for f in features]
    return pd.DataFrame(rows)


def tile_url(ee_image, vis_params):
    """يحوّل صورة GEE إلى رابط Tile (XYZ) يمكن لأي مكتبة خرائط ويب (Leaflet/Mapbox) استخدامه."""
    map_id = ee_image.getMapId(vis_params)
    return map_id["tile_fetcher"].url_format


# ----------------------------------------------------------------------------
# 2) منطقة الدراسة (تُحسب مرة واحدة وتُخزَّن مؤقتاً)
# ----------------------------------------------------------------------------
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
    if gedaref_state.size().getInfo() == 0:
        raise ValueError("تعذّر العثور على ولاية القضارف آلياً في طبقة GAUL.")

    adm1_name = gedaref_state.first().get("ADM1_NAME")
    localities = gaul2.filter(ee.Filter.eq("ADM1_NAME", adm1_name))
    return gedaref_state.geometry(), gedaref_state, localities


EPS = 1e-6


# ----------------------------------------------------------------------------
# 3) خط الأنابيب الكامل — نفس منطق الدفتر، بشكل دالة واحدة قابلة للاستدعاء
# ----------------------------------------------------------------------------
def run_pipeline(season_year: int, start_month: int = 6, end_month: int = 10):
    init_earth_engine()
    AOI, GEDAREF_STATE_FC, GEDAREF_LOCALITIES = load_gedaref_boundaries()

    RAIN_HIST_START_YEAR = 1991
    RAIN_HIST_END_YEAR = season_year - 1
    MODIS_HIST_START_YEAR = 2000
    MODIS_HIST_END_YEAR = season_year - 1

    SEASON_START = ee.Date.fromYMD(season_year, start_month, 1)
    SEASON_END = ee.Date.fromYMD(season_year, end_month, 1).advance(1, "month")

    _today = datetime.date.today()
    is_ytd = (season_year == _today.year) and (_today < datetime.date(season_year, end_month, 1))
    data_latency_adjusted = False
    if is_ytd:
        eff_end_month, eff_end_day = _today.month, _today.day
        SEASON_END = ee.Date.fromYMD(season_year, eff_end_month, eff_end_day)

        # ---- صمام أمان توفر البيانات (منقول من الدفتر الأصلي) ----
        # بيانات CHIRPS تُنشر بفجوة زمنية (latency) عن التاريخ الفعلي. لو طلبنا بيانات
        # "لحد اليوم" لكن آخر صورة منشورة أقدم من كده، هنحصل على مجموع أمطار ناقص بشكل
        # مصطنع (يفوّت آخر أسابيع فعلية) بينما السنوات التاريخية كاملة تماماً — ده بيضخّم
        # انحراف الأمطار سلباً بشكل غير حقيقي. نصحّح نقطة القطع تلقائياً لآخر بيانات متاحة.
        latest_chirps_img = (
            ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
            .filterBounds(AOI).sort("system:time_start", False).first()
        )
        chirps_latest_str = ee.Date(latest_chirps_img.get("system:time_start")).format("YYYY-MM-dd").getInfo()
        chirps_latest_date = datetime.date.fromisoformat(chirps_latest_str)
        requested_end_date = datetime.date(season_year, eff_end_month, eff_end_day)
        if chirps_latest_date < requested_end_date:
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
        .reduceRegion(reducer=ee.Reducer.mean(), geometry=AOI, scale=500, maxPixels=1e9)
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
    DRY_DEKAD_THRESHOLD_MM = 5

    def get_dekadal_rainfall_collection(year, s_month, e_month, aoi):
        start = ee.Date.fromYMD(year, s_month, 1)
        # نستخدم نفس نقطة القطع المصحّحة (eff_end_month/eff_end_day بعد صمام أمان توفر
        # البيانات) بدل "اليوم" الفعلي مباشرة، لتفادي نفس مشكلة فجوة نشر CHIRPS هنا أيضاً.
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
    GSCSI_WEIGHTS = {
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

    GSCSI_CLASS_LABELS = {1: "فشل", 2: "خطر", 3: "متوسط", 4: "جيد", 5: "ممتاز"}
    GSCSI_CLASS_PALETTE = ["a50026", "f46d43", "fee08b", "66bd63", "1a9850"]

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
        "gscsi": tile_url(GSCSI, gscsi_vis),
        "class": tile_url(GSCSI_CLASS, class_vis),
        "ndvi": tile_url(s2_current.select("NDVI"), ndvi_vis),
        "rainfall_anomaly": tile_url(rainfall_anomaly_pct, rain_anom_vis),
    }

    # ---------------- Zonal stats per locality ----------------
    zonal_image = (
        GSCSI.addBands(score_bands).addBands(GSCSI_CLASS)
        .addBands(rain_current_season.rename("Rain_Season_mm"))
        .addBands(rainfall_anomaly_pct)
        .addBands(s2_current)
        .addBands(VCI.rename("VCI")).addBands(TCI.rename("TCI"))
    )
    stats_fc = zonal_image.reduceRegions(
        collection=GEDAREF_LOCALITIES, reducer=ee.Reducer.mean(), scale=500, tileScale=8
    )
    df_stats = fc_to_df(stats_fc)
    if "GSCSI_Class" in df_stats.columns:
        df_stats["GSCSI_Class_Label"] = df_stats["GSCSI_Class"].round().map(GSCSI_CLASS_LABELS)
    keep_cols = [c for c in [
        "ADM2_NAME", "GSCSI", "GSCSI_Class_Label", "Rain_Season_mm", "Rainfall_Anomaly_Pct",
        "NDVI", "NDMI", "VCI", "TCI",
    ] if c in df_stats.columns]
    df_stats = df_stats[keep_cols].sort_values("GSCSI", ascending=False).reset_index(drop=True)
    localities = json.loads(df_stats.round(2).to_json(orient="records", force_ascii=False))

    # ---------------- NDVI current vs. historical climatology chart ----------------
    def add_mean_ndvi(img):
        mean_val = img.reduceRegion(reducer=ee.Reducer.mean(), geometry=AOI, scale=500, maxPixels=1e9).get("NDVI")
        return img.set("ndvi_mean", mean_val, "date_str", img.date().format("YYYY-MM-dd"))

    current_season_ndvi_col = MODIS_NDVI_COL.filterBounds(AOI).filterDate(SEASON_START, SEASON_END).map(add_mean_ndvi)
    hist_ndvi_col = (
        MODIS_NDVI_COL.filterBounds(AOI)
        .filterDate(ee.Date.fromYMD(MODIS_HIST_START_YEAR, start_month, 1),
                    ee.Date.fromYMD(MODIS_HIST_END_YEAR, end_month, 1).advance(1, "month"))
        .map(add_mean_ndvi)
    )
    current_dates = current_season_ndvi_col.aggregate_array("date_str").getInfo()
    current_vals = current_season_ndvi_col.aggregate_array("ndvi_mean").getInfo()
    hist_dates = hist_ndvi_col.aggregate_array("date_str").getInfo()
    hist_vals = hist_ndvi_col.aggregate_array("ndvi_mean").getInfo()

    df_current = pd.DataFrame({"date": pd.to_datetime(current_dates), "ndvi": current_vals})
    df_current["ndvi"] = df_current["ndvi"] * 0.0001
    df_current["doy"] = df_current["date"].dt.dayofyear

    df_hist = pd.DataFrame({"date": pd.to_datetime(hist_dates), "ndvi": hist_vals})
    df_hist["ndvi"] = df_hist["ndvi"] * 0.0001
    df_hist["doy"] = df_hist["date"].dt.dayofyear
    hist_climatology = df_hist.groupby("doy")["ndvi"].agg(["mean", "std"]).reset_index()

    ndvi_timeseries = {
        "current": json.loads(df_current[["doy", "ndvi"]].round(4).to_json(orient="records")),
        "historical": json.loads(hist_climatology.round(4).to_json(orient="records")),
    }

    # ---------------- Summary ----------------
    gscsi_mean_info = GSCSI.reduceRegion(reducer=ee.Reducer.mean(), geometry=AOI, scale=1000, maxPixels=1e9).getInfo()
    gscsi_mean = round(gscsi_mean_info.get("GSCSI", 0), 1) if gscsi_mean_info.get("GSCSI") is not None else None
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

    rain_mean_info = rain_current_season.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=AOI, scale=1000, maxPixels=1e9
    ).getInfo()
    rain_anom_info = rainfall_anomaly_pct.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=AOI, scale=1000, maxPixels=1e9
    ).getInfo()

    centroid = AOI.centroid(1).coordinates().getInfo()  # [lon, lat]

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
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        },
        "summary": {
            "gscsi_mean": gscsi_mean,
            "gscsi_class_label": gscsi_class_label,
            "rain_season_mm": round(rain_mean_info.get("precipitation", 0), 1) if rain_mean_info.get("precipitation") is not None else None,
            "rainfall_anomaly_pct": round(rain_anom_info.get("Rainfall_Anomaly_Pct", 0), 1) if rain_anom_info.get("Rainfall_Anomaly_Pct") is not None else None,
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
    }
