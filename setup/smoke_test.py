"""Проверка стенда: хосты созданы, Zabbix получает данные от test-server.

Запуск: python setup/smoke_test.py  (Zabbix на http://localhost:8070, test-server на портах 8090–8093)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("ZABBIX_URL", "http://localhost:8070")
import setup  # noqa: E402

WAIT_SECONDS = int(os.environ.get("SMOKE_WAIT_SECONDS", "180"))


def item_value(host, key):
    items = setup.call("item.get", {"host": host, "filter": {"key_": key}, "output": ["lastvalue", "state", "error"]})
    return items[0] if items else None


def main():
    setup.wait_for_api()
    setup.token = setup.call("user.login", {"username": setup.USER, "password": setup.PASSWORD})

    hosts = setup.call("host.get", {"output": ["host"], "selectHostGroups": ["name"]})
    stand = sorted(h["host"] for h in hosts if any(g["name"] == "test-server" for g in h["hostgroups"]))
    assert stand == sorted(setup.SERVICES), f"Ожидал хосты {sorted(setup.SERVICES)}, есть {stand}"
    print(f"Хосты стенда: {stand}")

    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        health = item_value("gateway", "testserver.health")
        rps = item_value("gateway", "testserver.rps")
        if health and rps and health["lastvalue"] == "UP" and float(rps["lastvalue"] or 0) > 0:
            print(f"gateway: здоровье {health['lastvalue']}, {rps['lastvalue']} запросов в секунду")
            print("OK")
            return
        print(f"Жду данных: health={health and (health['lastvalue'] or health['error'])}, "
              f"rps={rps and (rps['lastvalue'] or rps['error'])}")
        time.sleep(10)
    sys.exit("Zabbix не получил данные от test-server")


if __name__ == "__main__":
    main()
