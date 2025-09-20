FROM python:3.11-slim

# Базовые оптимизации Python и pip
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

# Утилиты: git (для auto_release.sh), curl (healthcheck), ca-certs
# Если используешь psycopg2, нужен libpq5 (рантайм). build-essential не ставим,
# т.к. лучше собирать колёсами или использовать psycopg[binary].
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git openssh-client curl ca-certificates libpq5 \
 && git config --global user.name "Kuzka Seller Bot" \
 && git config --global user.email "admin@kuzkabuh.ru" \
 && rm -rf /var/lib/apt/lists/*

# Нерутовый пользователь
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Слои зависимостей кешируются
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Копируем проект
COPY . .

# Делаем репозиторий "safe" для git внутри контейнера (скрипты релиза)
RUN git config --global --add safe.directory /app

# Права на проект
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Healthcheck на эндпоинт приложения
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Запуск uvicorn (можешь переопределить командой в docker-compose)
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
