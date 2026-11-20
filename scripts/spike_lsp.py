"""M2 spike (throwaway, kept for reference): talk raw LSP to Pyright and print what it says.

Usage: python scripts/spike_lsp.py <repo_dir>
Captures: server->client requests (+ shape of workspace/configuration), server capabilities,
whether publishDiagnostics carries `version`, and whether a clean file gets an empty publish.
"""
import asyncio, json, shutil, sys, time
from pathlib import Path


def frame(obj):
    b = json.dumps(obj).encode()
    return b"Content-Length: " + str(len(b)).encode() + b"\r\n\r\n" + b


async def read_msg(r):
    headers = {}
    while True:
        line = await r.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.decode().partition(":")
        headers[k.lower()] = v.strip()
    return json.loads(await r.readexactly(int(headers["content-length"])))


async def main(repo: str):
    repo_p = Path(repo).resolve()
    cmd = shutil.which("pyright-langserver") or shutil.which("pyright-python-langserver")
    print("server:", cmd)
    p = await asyncio.create_subprocess_exec(cmd, "--stdio", cwd=str(repo_p),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    log = []

    async def send(o):
        p.stdin.write(frame(o)); await p.stdin.drain()

    async def drain_err():
        while (l := await p.stderr.readline()):
            print("[stderr]", l.decode().rstrip()[:200])
    asyncio.create_task(drain_err())

    async def reader():
        while (m := await read_msg(p.stdout)) is not None:
            t = time.time()
            if "method" in m and "id" in m:
                print(f"SERVER-REQUEST {m['method']} params={json.dumps(m.get('params'))[:400]}")
                res = None
                if m["method"] == "workspace/configuration":
                    res = [{} for _ in m["params"]["items"]]
                await send({"jsonrpc": "2.0", "id": m["id"], "result": res})
            elif "method" in m:
                if m["method"] == "textDocument/publishDiagnostics":
                    pr = m["params"]
                    print(f"PUBLISH uri={pr['uri'][-20:]} version={pr.get('version','<absent>')} n={len(pr['diagnostics'])}")
                    log.append(pr)
                else:
                    print("NOTIFY", m["method"], json.dumps(m.get("params"))[:150])
            else:
                print("RESPONSE id", m["id"], json.dumps(m.get("result", m.get("error")))[:1500])
    asyncio.create_task(reader())

    root = repo_p.as_uri()
    await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "processId": None, "rootUri": root, "workspaceFolders": [{"uri": root, "name": "r"}],
        "capabilities": {"textDocument": {"publishDiagnostics": {"versionSupport": True},
                                          "synchronization": {"didSave": False}},
                         "workspace": {"configuration": True, "workspaceFolders": True}}}})
    await asyncio.sleep(3)
    await send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    clean, bad = (repo_p / "clean.py"), (repo_p / "bad.py")
    for f, txt in [(clean, "x: int = 1\n"), (bad, "print(undefined_thing)\n")]:
        await send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {
            "uri": f.as_uri(), "languageId": "python", "version": 1, "text": txt}}})
    await asyncio.sleep(4)
    print("--- change bad -> clean (does it publish empty?) ---")
    await send({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {
        "textDocument": {"uri": bad.as_uri(), "version": 2}, "contentChanges": [{"text": "print(1)\n"}]}})
    await asyncio.sleep(3)
    print("--- pull diagnostics probe ---")
    await send({"jsonrpc": "2.0", "id": 9, "method": "textDocument/diagnostic",
                "params": {"textDocument": {"uri": bad.as_uri()}}})
    await asyncio.sleep(2)
    await send({"jsonrpc": "2.0", "id": 99, "method": "shutdown", "params": None})
    await asyncio.sleep(1)
    await send({"jsonrpc": "2.0", "method": "exit"})
    await asyncio.wait_for(p.wait(), 10)

asyncio.run(main(sys.argv[1]))
