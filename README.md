# Kuzka Seller Bot

Телеграм-бот и веб-кабинет продавца Wildberries.

- Вход в кабинет по одноразовой ссылке (OTT) из бота
- Хранение WB API-ключа в БД **в зашифрованном виде**
- Интеграция с WB API: **seller-info** и **account/balance**
- Простая админка для установки webhook
- `/metrics` для Prometheus, `/healthz` для health-check

---

## Содержание

- [Архитектура](#архитектура)
- [Технологии](#технологии)
- [Быстрый старт](#быстрый-старт)
- [Переменные окружения](#переменные-окружения)
- [Как это работает (OTT и сессии)](#как-это-работает-ott-и-сессии)
- [Интеграция с Wildberries](#интеграция-с-wildberries)
- [Безопасность и шифрование](#безопасность-и-шифрование)
- [Команды и полезные операции](#команды-и-полезные-операции)
- [FAQ / Трюблшутинг](#faq--трюблшутинг)
- [Релизы и CHANGELOG](#релизы-и-changelog)
- [Лицензия](#лицензия)

---

## Архитектура

```
┌────────┐   webhook (HTTPS)   ┌──────────────────────┐
│Telegram│ ───────────────────▶ │ FastAPI + aiogram    │
└────────┘                      │  /tg/webhook/...     │
                                │  /login/tg?token=... │
                                │  /dashboard, /settings
                                └──────────┬───────────┘
                                           │
                      ┌────────────────────┴────────────────────┐
                      │               Nginx (TLS)               │
                      └────────────────────┬────────────────────┘
                                           │
          ┌────────────────────────────────┴─────────────────────────────────┐
          │                         Docker network                           │
          └─────────────────┬────────────────────────────┬───────────────────┘
                            │                            │
                        PostgreSQL                    Redis
                     (пользователи,             (одноразовые токены
                   зашифрованные ключи)             логина, OTT)
```

Директории (важное):
- `app/main.py` — FastAPI приложение
- `app/bot/bot.py` — aiogram-бот (роутер и хэндлеры)
- `app/web/templates/` — HTML-шаблоны (Dashboard, Settings)
- `app/security/crypto.py` — шифрование WB API-ключей
- `app/db/` — модели и база
- `nginx/` (если есть) — конфигурация nginx, проксирование к сервису `web`

---

## Технологии

- **Python 3.11**, **FastAPI**, **aiogram**
- **PostgreSQL**, **Redis**
- **Uvicorn**, **Nginx**
- **Prometheus client** (метрики)
- **cryptography** (шифрование ключей)
- Docker / docker compose

---

## Быстрый старт

```bash
git clone https://github.com/kuzkabuh/wb-bot.git
cd wb-bot

# 1) окружение
cp .env.example .env
# отредактируйте .env (см. раздел ниже)

# 2) поднять стек
docker compose up -d --build

# 3) проверка
curl -sS http://127.0.0.1:8000/healthz
# {"status":"ok"}

# 4) поставить Telegram webhook
ADM=$(grep ^ADMIN_TOKEN .env | cut -d= -f2)
BASE=$(grep ^PUBLIC_BASE_URL .env | cut -d= -f2)
curl -s -X POST -H "Authorization: Bearer $ADM" "$BASE/admin/set_webhook"
```

> Требования: Docker 20+, docker compose plugin, внешний домен с корректным DNS и TLS, если бот должен принимать вебхуки из интернета.

---

## Переменные окружения

`.env` (минимум):

```dotenv
# Telegram
TELEGRAM_BOT_TOKEN=7965...:AA...           # токен бота
TELEGRAM_WEBHOOK_SECRET=long-random-secret # секрет в заголовке вебхука

# База и Redis
DATABASE_URL=postgresql+psycopg2://postgres:postgres@db:5432/postgres
REDIS_URL=redis://redis:6379/0

# Внешний URL и путь вебхука (будет склеено base/path без двойных слэшей)
PUBLIC_BASE_URL=https://app.kuzkabuh.ru
WEBHOOK_PATH=/tg/webhook/random-secret-path

# Админ-токен для вызова /admin/set_webhook
ADMIN_TOKEN=another-long-random-secret

# Логи
LOG_LEVEL=INFO

# Ключ шифрования (см. ниже). ДОЛЖЕН начинаться с base64:
MASTER_ENCRYPTION_KEY=base64:MyvO01w9ivBo/aaUwKsQEfbyK3ARUR/lKEWMAI1aFhw=
```

Сгенерировать ключ:

```bash
python - <<'PY'
import os, base64
print("base64:"+base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
```

---

## Как это работает (OTT и сессии)

1. Пользователь в боте нажимает **«Настройки»** → бот генерирует одноразовый токен (OTT) и кладет его в Redis на **10 минут**.
2. Бот присылает ссылку: `https://<PUBLIC_BASE_URL>/login/tg?token=<OTT>`.
3. Пользователь открывает ссылку, сервер гасит токен (атомарно) и кладет `tg_id` в cookie-сессию.
4. Автоматический редирект в `/dashboard`.
5. В **/settings** пользователь сохраняет WB API-ключ (он шифруется и пишется в БД).

Повторные клики по той же ссылке допустимы **в течение ~60 секунд** (кеш «recent»), потом — только новый OTT.

---

## Интеграция с Wildberries

Используем **реальный** WB API-ключ (без «тестового контура»). Заголовок:  
`Authorization: Bearer <token>`

Эндпоинты:
- `GET https://common-api.wildberries.ru/api/v1/seller-info` — наименование продавца, SID
- `GET https://finance-api.wildberries.ru/api/v1/account/balance` — валюта и текущий баланс

Квоты (по описанию WB):
- seller-info — 1 запрос/мин (всплеск 10)
- balance — 1 запрос/мин (всплеск 1)

Бот показывает эти же данные по кнопке **«Профиль»**.

---

## Безопасность и шифрование

- WB API-ключ хранится как `user_credentials.wb_api_key_encrypted` + `salt`.
- Для шифрования используется мастер-ключ `MASTER_ENCRYPTION_KEY` (формат `base64:<urlsafe_base64_32bytes>`).
- **Важно:** данные, зашифрованные старым ключом, нельзя расшифровать после смены ключа. Если вы заменили `MASTER_ENCRYPTION_KEY`, пересохраните ключ в **Settings** или удалите запись в `user_credentials`.

Проблемы и решения:
- `cryptography.exceptions.InvalidTag` — почти всегда означает, что мастер-ключ в контейнере не совпадает с `.env`. Проверьте `printenv` внутри `web` и перезапишите ключ/данные.
- `{"detail":"invalid_or_expired_token"}` — OTT был погашен; запросите новую ссылку в боте.

---

## Команды и полезные операции

### Проверка состояния

```bash
# логи web
docker compose logs -f --tail=200 web

# health изнутри контейнера web
docker compose exec -T web curl -sS http://127.0.0.1:8000/healthz

# метрики
curl -sS http://127.0.0.1:8000/metrics | head
```

### Webhook

```bash
# поставить вебхук (склейка PUBLIC_BASE_URL + WEBHOOK_PATH)
ADM=$(grep ^ADMIN_TOKEN .env | cut -d= -f2)
BASE=$(grep ^PUBLIC_BASE_URL .env | cut -d= -f2)
curl -s -X POST -H "Authorization: Bearer $ADM" "$BASE/admin/set_webhook"

# статус у Telegram
TOKEN=$(grep ^TELEGRAM_BOT_TOKEN .env | cut -d= -f2)
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | jq
```

### Nginx ↔ web (частая причина 502)

Оба сервиса должны быть в **одном docker compose проекте и сети**, и upstream в nginx должен указывать на **service name** приложения, например `web:8000`.

Проверка из контейнера nginx:

```bash
docker compose exec -T nginx sh -lc 'apk add --no-cache curl bind-tools >/dev/null 2>&1 || true;   nslookup web 127.0.0.11;   curl -sS http://web:8000/healthz'
```

Если `Could not resolve host: web`:
- Проверьте, что сервис в `docker-compose.yml` действительно называется `web`.
- Убедитесь, что и `nginx`, и `web` в одной `default` сети compose.
- Полная «чистка» сети:
  ```bash
  docker compose down
  docker network prune -f
  docker compose up -d --build
  ```

---

## FAQ / Трюблшутинг

**Бот молчит / у Telegram 502 на вебхуке**  
— Проверьте `nginx → web` (см. выше), имя сервиса, и что `web` действительно слушает `0.0.0.0:8000`.

**401 `bad secret` на вебхуке**  
— Заголовок `X-Telegram-Bot-Api-Secret-Token` не совпал. Переустановите вебхук через `/admin/set_webhook` и проверьте `TELEGRAM_WEBHOOK_SECRET`.

**OTT не работает**  
— Срок жизни 10 минут, одноразово. Запросите новую ссылку.

**WB API ключ «не расшифровывается»**  
— 99% это неподходящий `MASTER_ENCRYPTION_KEY` внутри контейнера. Проверьте `docker compose exec -T web printenv MASTER_ENCRYPTION_KEY` и сравните с `.env`.

---

## Релизы и CHANGELOG

- Версионирование: **SemVer**
- Коммиты: **Conventional Commits**
- Каждая значимая версия сопровождается:
  - Обновлением `CHANGELOG.md`
  - Файлом `docs/releases/vX.Y.Z.md`
  - Аннотированным тегом `git tag -a vX.Y.Z -m "..."`

Пример:
```bash
git switch -c feat/wb-some-feature
# ...код, коммиты...
git push -u origin feat/wb-some-feature

git switch main
git pull --ff-only
git merge --no-ff feat/wb-some-feature -m "Merge: WB some feature"
git push origin main

VER=v0.8.3
git tag -a "$VER" -m "Some feature + fixes"
git push origin "$VER"
```

---

## Лицензия

MIT (можете заменить на свою, при необходимости).
