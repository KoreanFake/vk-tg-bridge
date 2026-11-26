FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Непривилегированный пользователь; /data — для БД (примонтируется том)
RUN useradd -m bridge && mkdir -p /data && chown bridge:bridge /data
USER bridge

CMD ["python", "-m", "app.main"]
