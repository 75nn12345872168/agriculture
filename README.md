# GSCSI — تطبيق ويب لنظام الإنذار المبكر الزراعي (ولاية القضارف)

تطبيق ويب كامل (Backend + Frontend) مبني من دفتر GSCSI الأصلي. لا يوجد "نموذج مدرب" يُحمَّل —
المؤشر يُحسب حيّاً من صور الأقمار الصناعية عبر Google Earth Engine في كل مرة يُفتح فيها التطبيق.

```
gscsi-app/
├── backend/          ← خادوم Flask يشغّل حسابات Earth Engine ويعيد JSON
│   ├── app.py
│   ├── gscsi_engine.py
│   ├── requirements.txt
│   └── Dockerfile
└── frontend/         ← صفحة ويب واحدة (HTML/CSS/JS) تعرض الخريطة والرسوم البيانية
    └── index.html
```

## لماذا جزءان (Backend + Frontend)؟

Google Earth Engine يحتاج **مصادقة سرّية** (Service Account) لا يمكن وضعها في صفحة HTML
يراها أي زائر. لذلك:
- **Backend** (خادوم): يحتفظ بالمفتاح السرّي، يتصل بـ Earth Engine، ويُرجع نتائج جاهزة (JSON).
- **Frontend** (الواجهة): صفحة ثابتة تستدعي الـ Backend وتعرض النتائج بخريطة ورسوم بيانية.
  يمكن استضافتها مجاناً على GitHub Pages.

---

## الخطوة 1: إنشاء Service Account لمشروع Earth Engine

1. افتحي [console.cloud.google.com](https://console.cloud.google.com) واختاري مشروع Earth Engine
   الخاص بك (نفس المشروع اللي كنتِ تستخدميه في Colab).
2. من القائمة: **IAM & Admin → Service Accounts → Create Service Account**.
3. أعطيه اسماً (مثلاً `gscsi-backend`) وامنحيه دور **Earth Engine Resource Viewer** (أو Editor).
4. بعد إنشائه: **Keys → Add Key → Create new key → JSON**. سيُحمَّل ملف JSON — احتفظي به سرّياً
   (لا ترفعيه أبداً على GitHub بشكل عام).
5. تأكدي أن هذا الحساب مُسجَّل ومصرَّح له في Earth Engine:
   [signup.earthengine.google.com](https://signup.earthengine.google.com) → **Register a
   Service Account** (أو تحققي أن مشروعك مفعَّل لـ Earth Engine API من Cloud Console).

## الخطوة 2: تشغيل الـ Backend محلياً (اختباري)

```bash
cd backend
pip install -r requirements.txt
export GEE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"
python app.py
```

افتحي `http://localhost:8080/api/gscsi?year=2026&start_month=6&end_month=10` في المتصفح.
أول طلب قد يأخذ 20–60 ثانية (حسابات Earth Engine)؛ الطلبات التالية لنفس الموسم تُقرأ من الكاش
لمدة 6 ساعات.

## الخطوة 3: نشر الـ Backend (رابط جاهز ودائم)

أسهل خيارات مجانية/رخيصة تدعم Docker وتشغيل Python طويل الأمد:

### الخيار أ) Render.com (الأسهل)
1. ادفعي مجلد `backend/` إلى مستودع GitHub منفصل (أو استخدمي نفس المستودع مع تحديد الجذر).
2. في Render: **New → Web Service** → اربطي المستودع.
3. Root Directory: `backend`. Build: يستخدم `Dockerfile` تلقائياً.
4. في **Environment**: أضيفي متغيّر `GEE_SERVICE_ACCOUNT_JSON` وألصقي فيه **محتوى** ملف
   JSON بالكامل (كنص واحد).
5. بعد النشر ستحصلين على رابط مثل: `https://gscsi-backend.onrender.com`.

### الخيار ب) Google Cloud Run
```bash
cd backend
gcloud run deploy gscsi-backend \
  --source . \
  --region europe-west1 \
  --set-env-vars GEE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)" \
  --memory 1Gi --timeout 120 --allow-unauthenticated
```
مناسب جداً لأنه نفس مزوّد Earth Engine (Google)، وعادة أسرع في الاتصال بـ GEE.

### الخيار ج) Railway.app
نفس فكرة Render: اربطي المستودع، حددي `backend` كجذر، أضيفي نفس متغيّر البيئة.

## الخطوة 4: نشر الـ Frontend على GitHub Pages (مجاني، سهل)

1. افتحي `frontend/index.html` وعدّلي هذا السطر بأول الملف (داخل `<script>`):
   ```js
   const API_BASE_URL = "https://gscsi-backend.onrender.com"; // رابط الـ backend من الخطوة 3
   ```
2. ارفعي مجلد `frontend/` إلى مستودع GitHub (يمكن أن يكون نفس المستودع أو مستودع منفصل باسم
   مثلاً `gscsi-dashboard`).
3. من إعدادات المستودع: **Settings → Pages → Source: Deploy from branch → اختاري الفرع والمجلد**
   (لو الملف داخل `frontend/`، اختاري `/frontend` كمجلد الجذر، أو انقلي `index.html` لجذر
   المستودع مباشرة لتبسيط الأمر).
4. بعد دقيقة أو اثنتين، رابط جاهز مثل:
   `https://<username>.github.io/gscsi-dashboard/`

هذا هو **الرابط الجاهز النهائي** اللي تقدري تشاركيه مع أي حد — يفتح لوحة تحكم كاملة بالخريطة
والمؤشر والجدول، بدون أي إعداد من طرف الزائر.

---

## ما الذي يعرضه التطبيق الآن (الهدف الأساسي)؟

- المؤشر المركب **GSCSI** الحالي (متوسط الولاية) بعقارب قياس ملوّن حسب التصنيف.
- خريطة تفاعلية بطبقات: GSCSI، التصنيف، NDVI، انحراف الأمطار.
- جدول مقارنة بين محليات القضارف مرتّب حسب GSCSI.
- رسم بياني: NDVI للموسم الحالي مقابل المتوسط التاريخي (2000–اليوم).
- ملخص أمطار الموسم وانحرافها عن المتوسط التاريخي، مع تمييز الموسم الجاري (YTD) تلقائياً.

## ما الذي لم يُنقَل بعد (اختياري لاحقاً)

الدفتر الأصلي يحتوي أيضاً على: التحقق من الأداء ومعايرة الأوزان (Leave-One-Out)، وتوقع الأمطار
المتبقية بطريقة السنوات المشابهة (Analog Ensemble + ENSO/RONI). هذه خلايا "بحثية" أثقل حسابياً
ومناسبة أكثر كتقرير سنوي دوري وليس كجزء من كل تحميل صفحة. يمكن إضافتها لاحقاً كنقطة API منفصلة
(مثلاً `/api/forecast`) تُحسب مرة واحدة شهرياً وتُخزَّن بدل حسابها لحظياً.

## ملاحظات أداء مهمة

- أول تحميل لكل موسم بطيء (20–60 ثانية) لأن Earth Engine يعالج آلاف الصور. الكاش الداخلي في
  `app.py` يجعل الطلبات التالية فورية لمدة 6 ساعات — يمكن تعديل `CACHE_TTL_SECONDS`.
- استضافات مجانية (Render Free) "تنام" بعد عدم الاستخدام وتحتاج ~30 ثانية لإعادة التشغيل عند أول
  زيارة يومية — طبيعي وليس خطأ.
