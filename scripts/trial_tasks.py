"""Drive requests through a running agent (used with scripts/browser_trial.py) and answer its cards.

  .venv/bin/python scripts/trial_tasks.py <token-file> "request 1" "request 2" ...

Approvals are answered yes. Choice cards are answered by a simple stand-in for the owner: for a
single-choice question it picks "Archive" if offered; otherwise it ticks options mentioning deals,
sales or newsletters. Prints what happened at each step.
"""
import asyncio
import json
import sys
import time

import httpx
import websockets

PORT = 8767


def owner_pick(e):
    opts = e["options"]
    if not e["multiple"]:
        return [o for o in opts if o.lower() == "archive"][:1]
    return [o for o in opts if any(w in o.lower() for w in ("deal", "sale", "newsletter", "today in tech"))]


async def run(c, token, msg):
    t = time.time()
    r = (await c.post("/chat", json={"message": msg})).json()["turn"]
    print(f"\n### {msg}\n  plan {time.time()-t:.0f}s by {r['planned_by']} | allowed: {r['scope_text']} | "
          f"escalated: {r['escalated']} | notes: {r['notes']} | error: {(r['error'] or '')[:300]}")
    if not r["plan"]:
        return
    print("  steps:", json.dumps(r["plan"]["steps"])[:900])
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/events") as ws:
        await ws.send(json.dumps({"token": token}))
        await ws.recv()
        started = await c.post(f"/plan/{r['plan_id']}/run")
        if started.status_code != 202:
            print("  NOT RUN:", started.json().get("detail"))
            return
        await follow(c, ws)


async def follow(c, ws):
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), 600))
            if e["type"] == "approval.request":
                print(f"  APPROVAL: {e['tool']} {e['args']}")
                await c.post(f"/approval/{e['approval_id']}", json={"decision": "yes"})
            elif e["type"] == "choice.request":
                pick = owner_pick(e)
                print(f"  CHOICE: {e['question']!r} options={e['options']} -> owner picks {pick}")
                await c.post(f"/choice/{e['choice_id']}", json={"selected": pick})
            elif e["type"] == "show":
                print("  SHOW:", str(e["value"])[:400].replace("\n", " | "))
            elif e["type"] == "audit":
                rec = e["record"]
                if rec["event"] == "tool.call" and rec["tool"] != "show":
                    print(f"  ok {rec['tool']} -> {str(rec.get('result'))[:200]}")
                elif rec["event"] in ("policy.refuse", "tool.error", "step.skip"):
                    print(f"  {rec['event']}: {str({k: rec.get(k) for k in ('tool', 'reason', 'error')})[:300]}")
            elif e["type"] == "run.end":
                print("  END:", e["status"], e["error"])
                break
            elif e["type"] == "state" and e.get("paused"):
                print("  STANDBY:", e.get("standby_reason"))


async def run_existing(c, token, plan_id):
    """Run a plan that was already made (prefix the argument with 'plan:')."""
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/events") as ws:
        await ws.send(json.dumps({"token": token}))
        await ws.recv()
        started = await c.post(f"/plan/{plan_id}/run")
        if started.status_code != 202:
            print("  NOT RUN:", started.json().get("detail"))
            return
        await follow(c, ws)


async def main():
    token = open(sys.argv[1]).read().strip()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", headers={"Authorization": f"Bearer {token}"},
                                 timeout=900) as c:
        for m in sys.argv[2:]:
            if m.startswith("plan:"):
                await run_existing(c, token, m[5:])
            else:
                await run(c, token, m)


asyncio.run(main())
