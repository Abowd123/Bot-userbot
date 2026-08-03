# ---------- مرحلة البناء: تُترك أدوات الترجمة خارج الصورة النهائية ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# TgCrypto قد يحتاج ترجمة إن لم يتوفر wheel للمعمارية
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ---------- الصورة النهائية ----------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Aden \
    DISPLAY_TZ=Asia/Aden \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

# مستخدم غير جذر: لا يحتاج البوت أي صلاحيات نظام
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --chown=appuser:appuser . .
USER appuser

# عملية عاملة (worker) لا تستمع على أي منفذ، فلا EXPOSE ولا healthcheck HTTP
CMD ["python", "-u", "main.py"]
