FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Устанавливаем git и полезные утилиты без дополнительных рекомендаций,
# затем очищаем списки пакетов, чтобы уменьшить размер образа.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client curl \
    && git config --global user.name "Kuzka Seller Bot" \
    && git config --global user.email "admin@kuzkabuh.ru" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала копируем requirements.txt, чтобы слои с зависимостями могли кешироваться
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Затем копируем весь проект
COPY . .

ENV PYTHONPATH=/app

EXPOSE 8000
