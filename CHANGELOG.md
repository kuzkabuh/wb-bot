# Changelog

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Этот проект следует [Semantic Versioning](https://semver.org/lang/ru/).

## [Unreleased]

### Добавлено
- ...

### Исправлено
- ...

---

## [v0.8.3] - 2025-09-19
### Added
- **Авторизация и регистрация через Telegram**: вход в кабинет по одноразовой ссылке (OTT) из бота, привязка сессии к `telegram_id`.
- Кнопка **«Профиль»** в боте: выдаёт одноразовую ссылку входа и ведёт в личный кабинет.
- Эндпойнт `GET /auth/whoami` для быстрого определения статуса сессии.
- Полноценный `README.md` с установкой, OTT-потоком и интеграцией с WB.
- Шаблон релиз-нотов в `docs/releases/` и обновляемый `CHANGELOG.md`.

### Changed
- Нормализация webhook URL при установке (`/admin/set_webhook`), чтобы исключить двойные слэши.
- Подчистка и уточнение примеров `nginx`-конфига и рекомендаций по Docker-сети.

### Fixed
- Случаи, когда кабинет открывался как «демо» без установленной сессии, при переходе со старых ссылок. Добавлен «второй шанс» (60 сек) на повторный вход по использованной OTT-ссылке.

### Ops/Docs
- Обновлён раздел «Типичные проблемы» (502 от nginx и DNS внутри Docker).
- Уточнены требования к зависимостям (`itsdangerous`, `cryptography`) и инструкции по пересборке.

## [v0.8.2] — 2025-09-19
### Добавлено
- Реальный **Dashboard** в кабинете: вывод имени продавца и торговой марки через `GET https://common-api.wildberries.ru/api/v1/seller-info` и баланса через `GET https://finance-api.wildberries.ru/api/v1/account/balance` (авторизация `Authorization: Bearer <token>`).
- Страница **Settings**: безопасное сохранение WB API Token (Fernet + AES-GCM, соль; ключ из `MASTER_ENCRYPTION_KEY`).
- **OTT-вход** из Telegram (одноразовая 10-минутная ссылка), cookie-сессии на базе `SessionMiddleware` и `itsdangerous`.
- **/metrics** (Prometheus).

### Изменено
- Улучшена навигация и сообщения бота для онбординга.

## [v0.8.1] — 2025-09-19
### Добавлено
- Кнопка **«Профиль»** в Telegram-боте и соответствующий хэндлер: получаем продавца и баланс (WB API).

## [v0.7.1] — 2025-09-19
### Изменено
- Минорные улучшения UX/логирования и обработка ошибок. *(служебный релиз)*

## [v0.6.2] — 2025-09-19
### Добавлено
- Починен `SessionMiddleware`: добавлен `itsdangerous`.
- Подготовлен **OTT login** и `/metrics` для мониторинга.

## [v0.4.3] — 2025-09-18
### Изменено
- Небольшие рефакторинги приложения и скриптов деплоя. *(служебный релиз)*

## [v0.4.2] — 2025-09-18
### Исправлено
- **Admin: set_webhook** — нормализован URL (убраны двойные слэши `//`), чтобы Telegram перестал получать `404` при `POST` на webhook.

## [v0.4.1] — 2025-09-18
### Изменено
- Техническая подготовка инфраструктуры. *(служебный релиз)*

## [v0.4.0] — 2025-09-18
### Добавлено
- Базовый скелет: **FastAPI** + **Aiogram** (webhook), **Docker Compose**: `web`, `nginx`, `redis`, `db`, `worker`, `beat`.
- Эндпоинты `/healthz`, webhook и начальные шаблоны для кабинета.


[Unreleased]: https://github.com/kuzkabuh/wb-bot/compare/v0.8.2...HEAD
[v0.8.2]: https://github.com/kuzkabuh/wb-bot/compare/v0.8.1...v0.8.2
[v0.8.1]: https://github.com/kuzkabuh/wb-bot/compare/v0.7.1...v0.8.1
[v0.7.1]: https://github.com/kuzkabuh/wb-bot/compare/v0.6.2...v0.7.1
[v0.6.2]: https://github.com/kuzkabuh/wb-bot/compare/v0.4.3...v0.6.2
[v0.4.3]: https://github.com/kuzkabuh/wb-bot/compare/v0.4.2...v0.4.3
[v0.4.2]: https://github.com/kuzkabuh/wb-bot/compare/v0.4.1...v0.4.2
[v0.4.1]: https://github.com/kuzkabuh/wb-bot/compare/v0.4.0...v0.4.1
