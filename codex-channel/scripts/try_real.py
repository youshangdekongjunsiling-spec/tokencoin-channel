"""One real inference through a temporary, single-use TokenCoin test channel.
Requires the isolated adapter to be running. Each invocation may consume quota.
No automatic retries; no project files, CLI sessions or tools are sent.
"""
from __future__ import annotations
import argparse, datetime, hashlib, json, os, sys, threading, time, urllib.error, urllib.request, uuid
from pathlib import Path
import channel
import setup_adapter as adapter

MARKER = "TOKENCOIN_REAL_OK"

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if args.evidence.exists(): raise ValueError("Choose a fresh evidence path; prior attempts are retained")
    root = adapter.default_root().resolve()
    _, cfg = adapter.verify(root)
    if sum(1 for p in (root / "auth").glob("*.json") if p.is_file()) != 1:
        raise ValueError("This probe requires exactly one authorized account in the isolated adapter")
    retry = cfg["routing"]["retry"]
    if retry["request-retry"] != 0 or retry["max-retry-credentials"] != 1:
        raise ValueError("Disable adapter retry rounds and additional credential attempts first")
    code, data = adapter.request(root, "/v1/models", True)
    if code != 200 or args.model not in {m.get("id") for m in json.loads(data).get("data", [])}:
        raise ValueError("Chosen model is absent from the adapter catalog")
    channel.OPENER = adapter.OPENER  # Local-only transport; no redirects or environment proxies.
    trial = root.parent / "trials" / uuid.uuid4().hex
    adapter.private_directory(trial)
    supplier, consumer = trial / "supplier", trial / "consumer"
    channel.initialize(supplier, "My authorized Codex", args.model, "subscription-adapter",
                       "http://127.0.0.1:" + str(adapter.PORT) + "/v1", "TOKENCOIN_TRIAL_ADAPTER_KEY")
    os.environ["TOKENCOIN_TRIAL_ADAPTER_KEY"] = cfg["access"]["api-keys"][0]
    try: gateway = channel.Gateway(("127.0.0.1", 0), supplier)
    finally: os.environ.pop("TOKENCOIN_TRIAL_ADAPTER_KEY", None)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True); worker.start()
    invite = None
    evidence = {"tested_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "backend": "subscription-adapter", "adapter_version": adapter.VERSION,
                "model_requested": args.model, "consumer": "demo HTTP client on same Windows host",
                "inference_posts": 0, "automatic_retries": 0, "tools_sent": 0, "project_files_sent": 0,
                "requested_max_output_tokens": 128, "output_limit_enforcement": "not_independently_verified",
                "response_completed": False, "exact_reply": False, "success": False}
    started = time.monotonic()
    try:
        invite = channel.invite(supplier, "http://127.0.0.1:" + str(gateway.server_port),
                                trial / "invitation.json", requests=1, minutes=5)
        channel.join(consumer, "self", trial / "invitation.json")
        peer = channel.peer_read(consumer, "self")
        body = {"model": args.model, "stream": True, "store": False, "tools": [], "tool_choice": "none",
                "instructions": "Reply with only the requested marker. Do not use tools.",
                "input": "Reply exactly: " + MARKER, "max_output_tokens": 128,
                "reasoning": {"effort": "low"}}
        request = urllib.request.Request(peer["base_url"] + "/v1/responses",
                  data=channel.dump(body).encode(), headers={"Authorization": "Bearer " + peer["token"],
                  "Content-Type": "application/json", "Idempotency-Key": uuid.uuid4().hex})
        print("Sending one real request through TokenCoin -> local adapter -> " + args.model, flush=True)
        evidence["inference_posts"] = 1
        chunks, types = [], set()
        with channel.OPENER.open(request, timeout=35) as response:
            evidence["http_status"] = response.status
            evidence["gateway_backend"] = response.headers.get("X-TokenCoin-Backend")
            for line in response:
                if not line.startswith(b"data:"): continue
                try: event = json.loads(line[5:])
                except ValueError: continue
                kind = event.get("type", "")
                if isinstance(kind, str): types.add(kind)
                if kind == "response.output_text.delta":
                    if "first_text_seconds" not in evidence: evidence["first_text_seconds"] = round(time.monotonic() - started, 3)
                    chunks.append(event.get("delta", ""))
                elif kind == "response.completed":
                    evidence["response_completed"] = True
                    resp = event.get("response", {})
                    evidence["response_status"] = resp.get("status")
                    # Only model identifier and integer usage fields are retained from the upstream response.
                    evidence["model_reported"] = resp.get("model")
                    usage = resp.get("usage") or {}
                    evidence["usage"] = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")
                                         if type(usage.get(k)) is int}
                    evidence["usage_source"] = "upstream_reported"
        answer = "".join(chunks).strip()
        evidence["exact_reply"] = answer == MARKER
        if evidence["exact_reply"]: evidence["reply"] = MARKER
        else: evidence["unexpected_reply_sha256"] = hashlib.sha256(answer.encode()).hexdigest()
        evidence["event_types"] = sorted(types)
    except urllib.error.HTTPError as error:
        evidence["http_status"] = error.code; error.close()
        evidence["failure"] = "request_rejected_no_retry"
    except (OSError, ValueError, urllib.error.URLError) as error:
        evidence["failure"] = type(error).__name__ + "; execution or quota may be unknown; not retried"
    finally:
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if invite:
            channel.revoke(supplier, invite["channel_id"])
            evidence["test_channel_revoked"] = True
        gateway.shutdown(); gateway.server_close(); worker.join(timeout=3)
        for _ in range(20):
            state = channel.local_status(supplier)
            if all(x["status"] != "running" for x in state["requests"]): break
            time.sleep(.05)
        evidence["gateway_requests"] = [{"status": x["status"], "usage": json.loads(x["usage"]) if x["usage"] else None}
                                        for x in state["requests"]]
        evidence["remaining_request_slots"] = state["channels"][0]["remaining"] if state["channels"] else None
        evidence["success"] = (evidence["response_completed"] and evidence["exact_reply"] and
                               len(state["requests"]) == 1 and state["requests"][0]["status"] == "completed_upstream")
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["success"] else 1

if __name__ == "__main__":
    try: raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print("Probe preflight failed (" + type(error).__name__ + "). No automatic retry.", file=sys.stderr)
        raise SystemExit(1)
