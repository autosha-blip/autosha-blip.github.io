import base64, json, time
from verify import Verifier, USDC_BASE

PAY_TO = "0xdD28076BdC986999f0240c03367Aaf0dB1728896"
now = int(time.time())

def hdr(d):
    return base64.b64encode(json.dumps(d).encode()).decode()

def payload(**over):
    auth = {"from":"0x1111111111111111111111111111111111111111",
            "to":PAY_TO,"value":"5000","asset":USDC_BASE,
            "validAfter":now-60,"validBefore":now+300,"nonce":"0xabc123"}
    auth.update(over.pop("auth",{}))
    d = {"x402Version":1,"network":"base","payload":{"authorization":auth}}
    d.update(over)
    return d

v = Verifier(PAY_TO, "0.005")   # 5000 atomic = 0.005 USDC

# happy path
ok,info = v.verify(hdr(payload()))
assert ok, info
# underpay
ok,why = v.verify(hdr(payload(auth={"value":"1"})))
assert not ok and "underpaid" in why, why
# wrong pay-to
ok,why = v.verify(hdr(payload(auth={"to":"0x2222222222222222222222222222222222222222"})))
assert not ok and "pay-to" in why, why
# wrong network
ok,why = v.verify(hdr(payload(network="ethereum")))
assert not ok and "network" in why, why
# expired
ok,why = v.verify(hdr(payload(auth={"validBefore":now-1})))
assert not ok and "expired" in why, why
# wrong asset
ok,why = v.verify(hdr(payload(auth={"asset":"0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"})))
assert not ok and "asset" in why, why
# replay (same nonce twice)
ok,_ = v.verify(hdr(payload(auth={"nonce":"0xreplay"})))
assert ok
ok,why = v.verify(hdr(payload(auth={"nonce":"0xreplay"})))
assert not ok and "replay" in why, why
# garbage
ok,why = v.verify("not-base64!!")
assert not ok, why
ok,why = v.verify(None)
assert not ok, why
print("all 9 verification tests passed — fail-closed behavior confirmed")
print("onchain_check_required keys:", list(info["onchain_check_required"].keys()))
