import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8080"


def call(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


name = "测试流程A"
q = urllib.parse.quote(name)

print("save   ", call("POST", "/api/workflows",
                      {"name": name, "speed": 0.5,
                       "blocks": [{"type": "wait", "value": 1}]}))
print("list   ", call("GET", "/api/workflows"))
print("get    ", call("GET", "/api/workflows/get?name=" + q))
print("current", call("POST", "/api/current",
                      {"name": name, "speed": 0.5,
                       "blocks": [{"type": "wait", "value": 1}]}))
print("voice  ", call("GET", "/api/voice"))
print("trigger", call("POST", "/api/voice/trigger", {}))

for i in range(30):
    st = call("GET", "/api/state")[1]
    print("state  ", st["running"], st["status"], st["now"], round(st["progress"], 2))
    if not st["running"] and i > 2:
        break
    time.sleep(0.3)

print("voice2 ", call("GET", "/api/voice"))
print("delete ", call("DELETE", "/api/workflows?name=" + q))
print("list2  ", call("GET", "/api/workflows"))
