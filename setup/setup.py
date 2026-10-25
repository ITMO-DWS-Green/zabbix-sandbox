"""Настройка Zabbix через API после первого запуска.

1. Хост «Zabbix server» опрашивает агента в контейнере zabbix-agent2, а не 127.0.0.1.
2. Группа test-server и по хосту на каждый сервис стенда: здоровье, запросы в секунду,
   доля 5xx и 4xx, задержка, текущий сценарий — плюс триггеры на них.

Только стандартная библиотека Python. Повторный запуск ничего не дублирует.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("ZABBIX_URL", "http://localhost:8070").rstrip("/") + "/api_jsonrpc.php"
USER = os.environ.get("ZABBIX_USER", "Admin")
PASSWORD = os.environ.get("ZABBIX_PASSWORD", "zabbix")
TEST_SERVER_HOST = os.environ.get("TEST_SERVER_HOST", "host.docker.internal")

# Сервисы test-server и их порты на хосте (см. compose.yaml в репозитории test-server).
SERVICES = {"gateway": 8090, "orders": 8091, "payments": 8092, "database": 8093}

HTTP_AGENT, DEPENDENT = 19, 18
FLOAT, CHAR, TEXT = 0, 1, 4
JSONPATH, JAVASCRIPT = 12, 21

token = None
request_id = 0


def call(method, params):
    global request_id
    request_id += 1
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}).encode()
    headers = {"Content-Type": "application/json-rpc"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(URL, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        answer = json.load(resp)
    if "error" in answer:
        raise RuntimeError(f"{method}: {answer['error'].get('data') or answer['error']}")
    return answer["result"]


def wait_for_api():
    for attempt in range(60):
        try:
            version = call("apiinfo.version", {})
            print(f"Zabbix API {version} доступен")
            return
        except (urllib.error.URLError, ConnectionError, RuntimeError) as e:
            print(f"Жду API ({attempt + 1}/60): {e}")
            time.sleep(5)
    sys.exit("Zabbix API так и не поднялся")


def point_server_host_to_agent():
    hosts = call("host.get", {"filter": {"host": ["Zabbix server"]}, "selectInterfaces": ["interfaceid", "dns", "useip"]})
    if not hosts:
        print("Хост «Zabbix server» не найден — пропускаю")
        return
    for iface in hosts[0]["interfaces"]:
        if iface["useip"] == "0" and iface["dns"] == "zabbix-agent2":
            print("Хост «Zabbix server» уже опрашивает zabbix-agent2")
            return
        call("hostinterface.update", {"interfaceid": iface["interfaceid"], "useip": 0, "dns": "zabbix-agent2", "ip": ""})
        print("Хост «Zabbix server» теперь опрашивает агента zabbix-agent2")


def ensure_group(name):
    groups = call("hostgroup.get", {"filter": {"name": [name]}})
    if groups:
        return groups[0]["groupid"]
    return call("hostgroup.create", {"name": name})["groupids"][0]


def js_ratio(field):
    return (
        "var s = JSON.parse(value).stats;"
        f" return s.requests > 0 ? s.{field} / s.requests : 0;"
    )


def create_service_host(service, port, groupid):
    if call("host.get", {"filter": {"host": [service]}}):
        print(f"Хост {service} уже есть — пропускаю")
        return
    hostid = call("host.create", {
        "host": service,
        "groups": [{"groupid": groupid}],
        "description": f"Сервис test-server на http://{TEST_SERVER_HOST}:{port}",
        "tags": [{"tag": "stand", "value": "test-server"}],
    })["hostids"][0]
    base = f"http://{TEST_SERVER_HOST}:{port}"

    # Здоровье: 503 тоже ответ (сценарии UNAVAILABLE и CRASH), поэтому его принимаем.
    call("item.create", {
        "hostid": hostid, "name": "Здоровье", "key_": "testserver.health", "type": HTTP_AGENT,
        "value_type": CHAR, "url": f"{base}/actuator/health", "status_codes": "200,503",
        "delay": "10s", "timeout": "3s",
        "preprocessing": [{"type": JSONPATH, "params": "$.status", "error_handler": 0, "error_handler_params": ""}],
    })

    # Статус стенда — основной источник: из него зависимые элементы берут метрики.
    master = call("item.create", {
        "hostid": hostid, "name": "Статус test-server (JSON)", "key_": "testserver.status", "type": HTTP_AGENT,
        "value_type": TEXT, "url": f"{base}/chaos/status", "delay": "5s", "timeout": "3s", "history": "1d",
    })["itemids"][0]

    dependent = [
        ("Запросов в секунду", "testserver.rps", FLOAT, "rps", JSONPATH, "$.stats.requests"),
        ("Доля 5xx", "testserver.error_rate", FLOAT, "", JAVASCRIPT, js_ratio("serverErrors")),
        ("Доля 4xx", "testserver.client_error_rate", FLOAT, "", JAVASCRIPT, js_ratio("clientErrors")),
        ("Средняя задержка", "testserver.latency", FLOAT, "ms", JSONPATH, "$.stats.avgLatencyMs"),
        ("Сценарий", "testserver.scenario", CHAR, "", JSONPATH, "$.scenario.type"),
    ]
    for name, key, value_type, units, prep_type, prep in dependent:
        call("item.create", {
            "hostid": hostid, "name": name, "key_": key, "type": DEPENDENT, "master_itemid": master,
            "value_type": value_type, "units": units,
            "preprocessing": [{"type": prep_type, "params": prep, "error_handler": 0, "error_handler_params": ""}],
        })

    triggers = [
        (f"{service}: сервис недоступен", f'last(/{service}/testserver.health)<>"UP"', 4),
        (f"{service}: нет ответа больше минуты", f"nodata(/{service}/testserver.health,1m)=1", 4),
        (f"{service}: доля 5xx выше 10%", f"avg(/{service}/testserver.error_rate,1m)>0.1", 3),
        (f"{service}: доля 4xx выше 20%", f"avg(/{service}/testserver.client_error_rate,1m)>0.2", 2),
        (f"{service}: задержка выше 1 с", f"avg(/{service}/testserver.latency,1m)>1000", 2),
    ]
    call("trigger.create", [
        {"description": d, "expression": e, "priority": p, "tags": [{"tag": "service", "value": service}]}
        for d, e, p in triggers
    ])
    print(f"Хост {service}: 7 элементов, {len(triggers)} триггеров — {base}")


def main():
    global token
    wait_for_api()
    token = call("user.login", {"username": USER, "password": PASSWORD})
    point_server_host_to_agent()
    groupid = ensure_group("test-server")
    for service, port in SERVICES.items():
        create_service_host(service, port, groupid)
    print("Готово: http://localhost:8070 (Admin / zabbix)")


if __name__ == "__main__":
    main()
