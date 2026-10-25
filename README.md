# zabbix-sandbox

Песочница Zabbix 7.0 LTS в Docker Compose: поднимается одной командой, сразу настроена и мониторит [test-server](https://github.com/ITMO-DWS-Green/test-server). Чтобы потыкать Zabbix, разобраться в его модели (хосты, элементы, триггеры, проблемы) и API, а потом проверять на нём своё расширение.

## Запуск

```bash
docker compose up -d
```

Через минуту-две откройте **http://localhost:8070**, вход — `Admin` / `zabbix`.

Ход автонастройки можно посмотреть так:

```bash
docker compose logs -f setup
```

## Вместе с test-server

Zabbix ходит к test-server на `host.docker.internal:8090–8093`, поэтому стенд достаточно запустить рядом:

```bash
# в папке test-server
docker compose up -d --build
```

Дальше включайте сбои на панели test-server (http://localhost:8090) и смотрите в Zabbix:
- **Monitoring → Problems** — открытые проблемы по триггерам;
- **Monitoring → Latest data**, фильтр по группе `test-server` — метрики и графики.

Если test-server не запущен, у его хостов сработает триггер «нет ответа больше минуты». Так и должно быть.

## Что внутри

| Контейнер | Что делает | Порт |
|---|---|---|
| `postgres` | База Zabbix | — |
| `zabbix-server` | Сбор данных, триггеры, проблемы | 10051 |
| `zabbix-web` | Веб-интерфейс и API | 8070 |
| `zabbix-agent2` | Агент, мониторит сам Zabbix server | — |
| `setup` | Один раз настраивает Zabbix через API и завершается | — |

Скрипт `setup/setup.py` делает две вещи:
1. Направляет встроенный хост «Zabbix server» на контейнер агента. По умолчанию там стоит 127.0.0.1, и в Docker агент так не находится.
2. Создаёт группу `test-server` и хосты `gateway`, `orders`, `payments`, `database`.

| Элемент | Откуда | Ключ |
|---|---|---|
| Здоровье | `/actuator/health`, поле `status` | `testserver.health` |
| Статус (JSON) | `/chaos/status` — источник для элементов ниже | `testserver.status` |
| Запросов в секунду | `stats.requests` | `testserver.rps` |
| Доля 5xx | `stats.serverErrors / stats.requests` | `testserver.error_rate` |
| Доля 4xx | `stats.clientErrors / stats.requests` | `testserver.client_error_rate` |
| Средняя задержка | `stats.avgLatencyMs` | `testserver.latency` |
| Сценарий | `scenario.type` | `testserver.scenario` |

| Триггер | Условие | Важность |
|---|---|---|
| Сервис недоступен | здоровье не `UP` | High |
| Нет ответа больше минуты | нет данных о здоровье 1 минуту | High |
| Доля 5xx выше 10% | среднее за 1 минуту | Average |
| Доля 4xx выше 20% | среднее за 1 минуту | Warning |
| Задержка выше 1 с | среднее за 1 минуту | Warning |

Пороги у триггеров ручные — ровно то, что наше расширение должно предлагать само.

Скрипт можно запускать повторно (`docker compose run --rm setup`): то, что уже есть, он не трогает.

## API

API доступно по адресу `http://localhost:8070/api_jsonrpc.php`. Пример — получить токен и список хостов:

```bash
TOKEN=$(curl -s localhost:8070/api_jsonrpc.php -H 'Content-Type: application/json-rpc' \
  -d '{"jsonrpc":"2.0","method":"user.login","params":{"username":"Admin","password":"zabbix"},"id":1}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"])')

curl -s localhost:8070/api_jsonrpc.php -H 'Content-Type: application/json-rpc' -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","method":"host.get","params":{"output":["host"]},"id":2}'
```

Методы, которые понадобятся расширению: `history.get` (история метрик), `item.get`, `trigger.create`, `problem.get`. Документация: [Zabbix API 7.0](https://www.zabbix.com/documentation/7.0/en/manual/api).

## Проверка

```bash
python3 setup/smoke_test.py
```

Тест проверяет, что хосты созданы и Zabbix получает данные от gateway. То же самое запускается в CI: он поднимает test-server, Zabbix и прогоняет тест.

## Сбросить всё

```bash
docker compose down -v
```

Ключ `-v` удаляет и базу; при следующем запуске Zabbix настроится с нуля.
