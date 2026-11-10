# Recruitment Assistant

## Назначение

Recruitment Assistant автоматизирует обработку вакансий: выполняет поиск резюме через HH и X-Ray, использует AI-оценку резюме и отправляет результаты через Gmail. Очередь задач хранится в PostgreSQL. Выполнение разделено между web, general worker и browser worker. Локальный запуск выполняется через Docker Compose.

## Структура репозитория

```text
app/
  core/                 базовая конфигурация, ошибки, безопасность и telemetry
  persistence/          PostgreSQL, очередь, операции и транзакции
  integrations/
    ai/                  интеграция AI
    gmail/               отправка почты через Gmail
    hh/                  интеграция HH и управление токенами
    xray/                X-Ray поиск
  browser/               управление browser sessions и shared screen
  services/              прикладные сервисы и workflow
  web/                   HTTP и ASGI слой
  workers/               фоновые worker-компоненты
entrypoints/
  web.py                 web entrypoint
  worker.py              worker entrypoint
scripts/
  backup/verify.py       создание и проверка резервной копии
  diagnostics/check_gmail.py
  operations/resolve_history.py
  setup/bootstrap_server.py
  setup/config_sync.py
tests/
  concurrency/           проверки параллелизма
  integration/           PostgreSQL integration tests
  regression/            offline regression tests
docker/                  контейнерный adapter и вспомогательные скрипты
browser_service/         отдельный browser service
ui/                      статические ресурсы интерфейса
deploy/                  локальные secrets и backups, не для публикации
docs/history/            исторические документы
```

Основные package paths:

- `app/core/`;
- `app/persistence/`;
- `app/integrations/`;
- `app/browser/`;
- `app/services/`;
- `app/web/`;
- `app/workers/`.

Python-файлов непосредственно в корне репозитория нет.

## Роли процессов

`docker/container_entry.py` поддерживает следующие роли:

- `web` запускает `entrypoints/web.py`;
- `general` запускает `entrypoints/worker.py` с lane `general`;
- `browser` запускает `entrypoints/worker.py` с lane `browser`;
- `check-gmail` запускает `scripts/diagnostics/check_gmail.py`;
- `smoke-browser` проверяет изоляцию browser contexts;
- `bootstrap` запускает `scripts/setup/bootstrap_server.py`;
- `backup` однократно запускает `scripts/backup/verify.py`;
- `backup-loop` периодически запускает `scripts/backup/verify.py`.

## Требования

- Windows PowerShell 5.1 или новее.
- Docker Desktop в режиме Linux containers.
- Docker Compose v2.
- Интернет для первой сборки образов и загрузки зависимостей.

При обычном запуске через `START_LOCAL.ps1` приложение и зависимости выполняются внутри Docker-контейнеров. Локальный Python на Windows для этого запуска не требуется.

## Конфигурация и secrets

Файл `.env` располагается рядом с `START_LOCAL.ps1`. Файл `docker.local.env` используется Docker Compose. Каталог `deploy/secrets` содержит runtime secrets.

Не публикуйте `.env` и `deploy/secrets`, secrets нельзя хранить в Git. При использовании существующего PostgreSQL volume сохраняйте соответствующие существующие `deploy/secrets`: замена только `.env` не восстанавливает пароли существующей базы.

Не выполняйте `docker compose down -v` без осознанного решения удалить данные. Volumes нельзя удалять без backup и явного решения.

## Локальный запуск

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\START_LOCAL.ps1
```

`START_LOCAL.ps1` проверяет Docker engine, собирает application image и browser image, подготавливает локальные secrets, валидирует Compose, останавливает application writers перед bootstrap, запускает PostgreSQL, выполняет bootstrap, синхронизирует runtime configuration, запускает browser, web, worker-general и worker-browser, проверяет browser isolation и выводит статус сервисов.

После запуска:

- приложение: `http://localhost:8765`;
- health endpoint: `http://localhost:8765/health/live`.

## Остановка

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\STOP_LOCAL.ps1
```

Команда останавливает runtime services без удаления Docker volumes.

## Обновление только web

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\UPDATE_WEB.ps1
```

Команда пересобирает web service и пересоздаёт web container. PostgreSQL и workers этой командой не пересоздаются.

## Временный внешний доступ

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\START_TUNNEL.ps1
```

Требуется установленный `cloudflared`. Скрипт проверяет `/health/live`, затем создаёт tunnel к `127.0.0.1:8765`.

Публичный URL tunnel не заменяет authentication и access control. Используйте только согласованные тестовые данные. Не публикуйте реальные резюме через незащищённый стенд.

## Параллелизм

Для текущей локальной Compose-конфигурации:

- `GENERAL_CONSUMERS`: 16;
- `XRAY_CONSUMERS`: 8;
- `BROWSER_MAX_SESSIONS`: 8;
- queue backend: PostgreSQL.

Эти значения относятся к текущей локальной конфигурации и не являются универсальными production-лимитами.

## Тесты

### Offline regression

PowerShell:

```powershell
python -m pytest -p no:cacheprovider -q tests/concurrency tests/regression
```

Запуск не должен требовать внешней сети. Последний structural gate фактически прошёл с 183 tests. Число тестов может изменяться, основной критерий успеха: exit code 0.

### PostgreSQL integration

PowerShell:

```powershell
python -m pytest -p no:cacheprovider -q tests/integration/test_multi_recruiter_postgres.py
```

Нужен отдельный disposable PostgreSQL. Используются `TEST_DATABASE_ADMIN_URL` и `TEST_DATABASE_APP_URL`. Тест содержит safety checks для host, database и container prefix. Integration tests запрещено направлять на production database. Последний structural gate фактически прошёл с 11 tests. Основной критерий успеха: exit code 0.

## Docker test image

```powershell
docker build --target test -t recruitment-test .
```

Offline-запуск:

```powershell
docker run --rm --network none `
  --tmpfs /tmp:rw,nosuid,nodev,size=256m `
  --env RECRUITMENT_DB_PATH=/tmp/recruitment-tests.db `
  --env LOG_DIR=/tmp/recruitment-logs `
  --env APP_ENV_FILE=/tmp/recruitment-test.env `
  --env TASK_QUEUE_BACKEND=local `
  --env PYTHONDONTWRITEBYTECODE=1 `
  --entrypoint python `
  recruitment-test `
  -m pytest -p no:cacheprovider -q `
  tests/concurrency tests/regression
```

## Compose validation

```powershell
docker compose --env-file docker.local.env config --quiet
```

## Проверка синтаксиса

```powershell
$PyFiles = @(git ls-files '*.py')
python -m py_compile $PyFiles
```

## Backup verification

Роль `backup-loop` вызывает `scripts/backup/verify.py`. Maintenance image содержит PostgreSQL client 16. Каталог `./deploy/backups` монтируется в `/backups`. Verify script создаёт backup и выполняет restore verification согласно текущей реализации.

Для ручного одноразового запуска необходимо переопределить service command:

```powershell
docker compose --env-file docker.local.env --profile maintenance run --rm backup backup
```

Это не является описанием полного disaster recovery procedure.

## Operational scripts

- `scripts/backup/verify.py`;
- `scripts/diagnostics/check_gmail.py`;
- `scripts/operations/resolve_history.py`;
- `scripts/setup/bootstrap_server.py`;
- `scripts/setup/config_sync.py`.

Эти scripts обычно запускаются контейнерным adapter или оператором в контролируемом окружении.

## Безопасность

- Secrets нельзя хранить в Git или публиковать.
- Реальные резюме и персональные данные нельзя публиковать.
- Integration tests запрещено направлять на production database.
- Volumes нельзя удалять без backup и явного решения.
- Внешний tunnel не заменяет authentication и access control.
- Application containers используют read-only filesystem, `cap_drop` и `no-new-privileges` согласно `compose.yaml`.
- Сети `database` и `browsernet` являются internal networks.
- Сеть `egress` используется сервисами, которым нужен внешний доступ.

Это описание не является формальной security certification.

## Исторические документы

`docs/history/PARALLEL_AND_ERROR_FIXES.md` содержит историческую запись предыдущего релиза и не является актуальной инструкцией по установке.

## Статус структурной реорганизации

Runtime-код распределён по `app/*`, entrypoints находятся в `entrypoints/`, operational scripts находятся в `scripts/`, tests объединены в `tests/`. Python-файлов непосредственно в корне нет.

Фактически пройдены structural gates:

- Python syntax checks;
- Docker test image build;
- entrypoint import checks;
- 183 offline tests;
- 11 PostgreSQL integration tests;
- Docker Compose validation;
- `git diff --check`.

Это не production certification. В рамках последнего structural gate не выполнялись реальные end-to-end вызовы HH, Gmail, AI API или Playwright provider flows.
