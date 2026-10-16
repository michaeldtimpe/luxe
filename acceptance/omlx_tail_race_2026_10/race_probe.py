"""oMLX <4096 store-race probe.

A = [system(nonce + ~N tokens filler), user]; B = A + assistant + user, sent
the instant A returns (an agent step after a fast tool). With a hybrid model
the only reusable state for B is A's end-of-prompt (tail) snapshot, stored
asynchronously; B misses when it is admitted before that store lands.
Prints per-trial cached/prompt for B and the miss rate.
"""
import json, os, sys, time, uuid
import httpx

URL = "http://127.0.0.1:8000/v1/chat/completions"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "Qwen3.6-35B-A3B-4bit"
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 12
DELAY = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
WORDS = int(sys.argv[3]) if len(sys.argv) > 3 else 1800


def key():
    if os.environ.get("OMLX_API_KEY"):
        return os.environ["OMLX_API_KEY"]
    for line in open(os.path.expanduser("~/.luxe/secrets.env")):
        line = line.strip()
        if line.startswith("OMLX_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no OMLX_API_KEY")


H = {"Authorization": f"Bearer {key()}"}
filler = " ".join(f"item{i % 97} alpha beta" for i in range(WORDS // 3))


def chat(client, messages):
    r = client.post(URL, headers=H, json={
        "model": MODEL, "messages": messages, "max_tokens": 4,
        "temperature": 0, "stream": False}, timeout=600)
    r.raise_for_status()
    return r.json()


with httpx.Client() as c:
    misses = 0
    for t in range(TRIALS):
        sys_msg = {"role": "system", "content": f"[{uuid.uuid4()}] reference: {filler}"}
        a_msgs = [sys_msg, {"role": "user", "content": "Reply with ok."}]
        a = chat(c, a_msgs)
        b_msgs = a_msgs + [{"role": "assistant", "content": "ok"},
                           {"role": "user", "content": "Reply with ok again."}]
        time.sleep(DELAY)
        b = chat(c, b_msgs)
        ua, ub = a["usage"], b["usage"]
        cached = (ub.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        miss = cached == 0
        misses += miss
        print(f"trial {t:2d}: A prompt={ua['prompt_tokens']:5d}  "
              f"B prompt={ub['prompt_tokens']:5d} cached={cached:5d} {'MISS' if miss else 'hit'}",
              flush=True)
    print(f"misses {misses}/{TRIALS}")
