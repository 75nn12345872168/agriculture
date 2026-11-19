FROM python:3.11-slim

# إنشاء مستخدم غير جذري
RUN adduser --disabled-password --gecos "" appuser

WORKDIR /app

# متوافق مع سياق البناء عند ضبط Root Directory = backend في Render
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# نسخ محتويات مجلد backend داخل حزمة /app/backend لتوحيد مسار الاستيراد backend.app:app
COPY . ./backend/

# الحفاظ على صلاحيات /tmp القياسية (1777) وإنشاء مجلد كاش خاص بالتطبيق فقط
RUN chown -R appuser:appuser /app && \
    mkdir -p /tmp/gscsi_cache && \
    chown -R appuser:appuser /tmp/gscsi_cache

USER appuser

ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV GSCSI_CACHE_DIR=/tmp/gscsi_cache
EXPOSE 8080

# worker واحد + threads لتفادي انقسام الكاش/المهام بين العمليات، مع احترام متغير PORT من Render
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 180 --graceful-timeout 30 backend.app:app"]
