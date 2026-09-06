FROM python:3.11-slim

# إنشاء مستخدم غير جذري
RUN adduser --disabled-password --gecos "" appuser

WORKDIR /app

# تثبيت الاعتماديات أولاً للاستفادة من cache
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# نسخ الكود
COPY backend/ ./backend/
COPY vendor/ ./vendor/
COPY index.html ./index.html
COPY README.md ./README.md

RUN chown -R appuser:appuser /app && mkdir -p /tmp && chown appuser:appuser /tmp
USER appuser

ENV PORT=8080
ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# worker واحد + threads لتفادي انقسام الكاش/المهام بين العمليات
CMD ["gunicorn","--bind","0.0.0.0:8080","--workers","1","--threads","4","--timeout","180","--graceful-timeout","30","backend.app:app"]
