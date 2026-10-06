import json, os, sys, time, collections, re
import httpx
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
src = open("/app/backend/rh_discovery.py").read()
TB = re.search(r'^T_BUY\s*=\s*"(0x[0-9a-fA-F]+)"', src, re.M).group(1)
TS = re.search(r'^T_SELL\s*=\s*"(0x[0-9a-fA-F]+)"', src, re.M).group(1)
URL = os.environ["RH_RPC_URL"]
c = httpx.Client(timeout=60)
head = int(c.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}).json()["result"], 16)
for span in [int(a) for a in sys.argv[1:]] or [18000, 60000]:
    t0 = time.time()
    r = c.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [{"fromBlock": hex(head - span), "toBlock": "latest", "topics": [[TB, TS]]}]})
    d = r.json()
    if "result" not in d:
        print(span, "err", d.get("error"), r.status_code); continue
    cnt = collections.Counter(l["address"].lower() for l in d["result"])
    print(f"span {span} ({span/600:.0f} min): {len(d['result'])} trade logs, {len(cnt)} curves, {time.time()-t0:.1f}s, {len(r.content)//1024} KB; curves with >=5 trades: {sum(1 for v in cnt.values() if v >= 5)}")
