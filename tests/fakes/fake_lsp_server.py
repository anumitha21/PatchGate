"""Scripted fake LSP server (stdio, Content-Length framing) for unit tests.

Configured by a JSON file whose path is in env FAKE_LSP_SCENARIO:
  log: path; every received message is appended there as one JSON line
  versions: bool (default true)  include `version` in publishDiagnostics
  delay_ms: int                  delay before publishing (per document change)
  preliminary_empty_ms: int      publish an EMPTY list first, then the real diagnostics after this delay
  drop: bool                     never publish (to test timeouts)
  rules: [{"contains": str, "message": str, "code": str, "severity": int}]
         a diagnostic is produced on every line containing `contains`
  config_items: int              after `initialized`, send workspace/configuration with N items
  unknown_request: bool          after `initialized`, send an unknown server->client request
  exit_after_opens: int          exit abruptly after N didOpen/didChange notifications
  xrules: [{"file": "user.py", "contains": "f(1)", "other_file": "lib.py", "other_contains": "def f(a, b)",
            "message": str, "code": str}]
         cross-file: a diagnostic on `file` (line containing `contains`) exists iff the CURRENT text of
         `other_file` contains `other_contains`; republished for `file` whenever either document changes.
"""
import json
import os
import sys
import threading
import time

sc = json.load(open(os.environ["FAKE_LSP_SCENARIO"]))
out = sys.stdout.buffer
inp = sys.stdin.buffer
wlock = threading.Lock()
count = {"n": 0}
docs = {}  # uri -> (version, text)


def log(obj):
    if sc.get("log"):
        with open(sc["log"], "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def send(obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    with wlock:
        out.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        out.flush()


def read():
    length = None
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.decode().partition(":")
        if k.lower() == "content-length":
            length = int(v)
    return json.loads(inp.read(length).decode("utf-8"))


def diags_for(text):
    res = []
    for i, line in enumerate(text.split("\n")):
        for r in sc.get("rules", []):
            if r["contains"] in line:
                res.append({"range": {"start": {"line": i, "character": 0}, "end": {"line": i, "character": 1}},
                            "severity": r.get("severity", 1), "code": r.get("code"), "message": r["message"]})
    return res


def xdiags(uri):
    res = []
    for r in sc.get("xrules", []):
        if not uri.endswith("/" + r["file"]):
            continue
        other = next((t for u, (_, t) in docs.items() if u.endswith("/" + r["other_file"])), None)
        if other is None or r["other_contains"] not in other:
            continue
        for i, line in enumerate(docs[uri][1].split("\n")):
            if r["contains"] in line:
                res.append({"range": {"start": {"line": i, "character": 0}, "end": {"line": i, "character": 1}},
                            "severity": 1, "code": r.get("code"), "message": r["message"]})
    return res


def publish(uri, version, diags):
    p = {"uri": uri, "diagnostics": diags}
    if sc.get("versions", True):
        p["version"] = version
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": p})


def schedule(uri, version, text):
    if sc.get("drop"):
        return

    def run():
        pre = sc.get("preliminary_empty_ms")
        if pre:
            publish(uri, version, [])
            time.sleep(pre / 1000)
        elif sc.get("delay_ms"):
            time.sleep(sc["delay_ms"] / 1000)
        publish(uri, version, diags_for(text) + xdiags(uri))
    threading.Thread(target=run, daemon=True).start()


def republish_dependents(changed_uri):
    for r in sc.get("xrules", []):
        if changed_uri.endswith("/" + r["other_file"]):
            for u, (v, t) in list(docs.items()):
                if u.endswith("/" + r["file"]):
                    schedule(u, v, t)


while True:
    m = read()
    if m is None:
        break
    log(m)
    method = m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"capabilities": {"textDocumentSync": 1}}})
    elif method == "initialized":
        if sc.get("config_items"):
            send({"jsonrpc": "2.0", "id": 9001, "method": "workspace/configuration",
                  "params": {"items": [{"section": f"s{i}"} for i in range(sc["config_items"])]}})
        if sc.get("unknown_request"):
            send({"jsonrpc": "2.0", "id": 9002, "method": "custom/unknownThing", "params": {}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": m["id"], "result": None})
    elif method == "textDocument/didClose":
        docs.pop(m["params"]["textDocument"]["uri"], None)
    elif method == "exit":
        break
    elif method == "textDocument/didOpen":
        td = m["params"]["textDocument"]
        count["n"] += 1
        docs[td["uri"]] = (td["version"], td["text"])
        schedule(td["uri"], td["version"], td["text"])
        republish_dependents(td["uri"])
    elif method == "textDocument/didChange":
        td = m["params"]["textDocument"]
        count["n"] += 1
        docs[td["uri"]] = (td["version"], m["params"]["contentChanges"][-1]["text"])
        schedule(td["uri"], td["version"], m["params"]["contentChanges"][-1]["text"])
        republish_dependents(td["uri"])
    if sc.get("exit_after_opens") and count["n"] >= sc["exit_after_opens"]:
        os._exit(1)
