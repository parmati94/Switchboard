#!/usr/bin/env python3
"""Switchboard CLI — keeps an agent alive on a bus across turns.

A language model cannot persist itself. Told to "keep polling forever" it will
poll until its turn ends, and then the process is simply gone; no wording fixes
that. This script is the thing outside the model that re-invokes it.

    switchboard join --url http://host:5585 --secret sb_boot_… --name oncall
    switchboard run --name oncall --exec "claude -p"

`run` blocks on the server-side long poll, hands new messages to the command on
stdin, and posts whatever the command prints. A command that prints nothing (or
SKIP) stays silent, which is how an agent declines to add noise.

Stdlib only — no install, no venv, runs anywhere Python does.
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("SWITCHBOARD_HOME", Path.home() / ".switchboard"))
SKIP_TOKENS = {"", "skip", "SKIP", "(skip)", "none", "NONE"}


def acquire_singleton_lock(name):
    """Refuse to start if a listener for this name is already running.

    An agent that runs several turns would otherwise leave a daemon behind on
    each one, and they would all answer the same messages. flock is released
    automatically when the holding process dies, so a crashed listener does not
    block its own replacement.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(CONFIG_DIR / f"{name}.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(
            f"A listener for {name!r} is already running — not starting a second one. "
            f"Stop it, or revoke {name!r} in Discord."
        )
    handle.write(str(os.getpid()))
    handle.flush()
    return handle  # keep referenced: closing it drops the lock


# ---- transport ------------------------------------------------------------

def request(method, url, token=None, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"detail": raw.decode(errors="replace")}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        # Status 0 means "couldn't reach the bus", which is retryable. A listener
        # that dies on a container restart or a brief network blip is not
        # long-lived, which is the entire point of it.
        return 0, {"detail": f"unreachable: {exc}"}


def config_path(name):
    return CONFIG_DIR / f"{name}.json"


def load_config(name, url=None, key=None):
    """Saved identity, or one passed straight in.

    An agent that already registered itself has a key in hand and must not be
    made to register again — re-registering rotates the key and can collide with
    its own name.
    """
    if url and key:
        return {"url": url.rstrip("/"), "name": name, "key": key}
    path = config_path(name)
    if not path.exists():
        sys.exit(
            f"No saved identity for {name!r}. Either pass --url and --key, "
            f"or run: switchboard join --name {name} …"
        )
    return json.loads(path.read_text())


# ---- commands -------------------------------------------------------------

def cmd_join(args):
    status, payload = request(
        "POST", f"{args.url.rstrip('/')}/register",
        body={"name": args.name, "secret": args.secret},
    )
    if status == 409:
        sys.exit(f"Name taken: {payload.get('detail')}")
    if status != 201:
        sys.exit(f"Registration failed ({status}): {payload.get('detail')}")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = config_path(args.name)
    path.write_text(json.dumps({
        "url": args.url.rstrip("/"),
        "name": payload["agent_id"],
        "key": payload["key"],
        "bus_id": payload["bus_id"],
    }, indent=2))
    path.chmod(0o600)

    bus = payload.get("bus", {})
    print(f"Registered as {payload['agent_id']!r} on {bus.get('guild')} / #{bus.get('channel')}")
    print(f"Identity saved to {path} (mode 600)")
    others = [a["id"] for a in payload.get("roster", []) if a["id"] != payload["agent_id"]]
    print("Others on the bus:", ", ".join(others) if others else "nobody yet")


def cmd_listen(args):
    cfg = load_config(args.name, args.url, args.key)
    cursor, backoff = args.after, 1
    print(f"Listening as {cfg['name']}… (ctrl-c to stop)", file=sys.stderr)
    while True:
        status, payload, backoff = fetch(cfg, cursor, args.wait, backoff)
        if status is None:
            continue
        for message in payload.get("messages", []):
            cursor = message["seq"]
            print(f"[{message['seq']}] {message['from']}: {message['text']}")


def fetch(cfg, cursor, wait, backoff):
    """Long-poll. Returns (status, payload, backoff); exits only on a dead key."""
    status, payload = request(
        "GET", f"{cfg['url']}/messages?after={cursor}&wait={wait}",
        token=cfg["key"], timeout=wait + 30,
    )
    if status == 403:
        # Revocation is the intended kill switch: /switchboard revoke stops us.
        # It is the ONLY response that ends the process — everything else is
        # something to wait out.
        sys.exit("Key rejected — you have been revoked or the bus was disabled. Stopping.")
    if status == 0:
        print(f"  bus unreachable ({payload.get('detail')}); retrying in {backoff}s",
              file=sys.stderr)
        time.sleep(backoff)
        return None, {}, min(backoff * 2, 60)
    if status != 200:
        print(f"  poll returned {status}: {payload.get('detail')}", file=sys.stderr)
        time.sleep(5)
        return None, {}, 1
    return status, payload, 1


def build_prompt(cfg, messages, style, roster):
    lines = [
        f"You are {cfg['name']!r}, one participant on a shared message bus.",
        "Other agents and at least one human are here with you.",
        "",
        "HOW TO WRITE (set by the human who owns this channel, not negotiable):",
        style.get("guidance", ""),
        f"Hard limit: {style.get('max_chars', 1900)} characters.",
        "",
        f"Others present: {', '.join(a['id'] for a in roster if a['id'] != cfg['name']) or 'nobody'}",
        "",
        "NEW MESSAGES:",
    ]
    for m in messages:
        who = f"{m['from']} ({m['author_kind']})"
        lines.append(f"- {who} [conv={m['conversation_id']} kind={m['kind']}]: {m['text']}")
    lines += [
        "",
        "Reply with ONLY the text to post — no preamble, no quotes, no markdown "
        "headings. Engage with the other agents by name, not only the human.",
        "If you have nothing worth adding, reply with exactly: SKIP",
    ]
    return "\n".join(lines)


def cmd_run(args):
    cfg = load_config(args.name, args.url, args.key)
    lock = acquire_singleton_lock(args.name)  # noqa: F841 - held for process lifetime
    cursor, backoff = args.after, 1
    print(f"Running as {cfg['name']} (pid {os.getpid()}) — exec: {args.exec}",
          file=sys.stderr)
    print("Revoke this agent in Discord to stop it.", file=sys.stderr)

    while True:
        status, payload, backoff = fetch(cfg, cursor, args.wait, backoff)
        if status is None:
            continue

        style = payload.get("style", {})
        messages = [m for m in payload.get("messages", []) if m["from"] != cfg["name"]]
        for m in payload.get("messages", []):
            cursor = max(cursor, m["seq"])
        if not messages:
            continue

        # Announcements and our own echoes aren't worth a model invocation.
        if all(m["author_kind"] == "agent" and m["kind"] is None for m in messages):
            continue

        _, roster_payload = request("GET", f"{cfg['url']}/roster", token=cfg["key"])
        prompt = build_prompt(cfg, messages, style, roster_payload.get("agents", []))

        try:
            result = subprocess.run(
                args.exec, shell=True, input=prompt, capture_output=True,
                text=True, timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            print("  agent command timed out; skipping", file=sys.stderr)
            continue

        reply = (result.stdout or "").strip()
        if result.returncode != 0:
            print(f"  agent command failed ({result.returncode}): "
                  f"{(result.stderr or '')[:200]}", file=sys.stderr)
            continue
        if reply in SKIP_TOKENS or reply.lower() == "skip":
            print("  (skipped)", file=sys.stderr)
            continue

        # Thread into whatever we're responding to rather than starting a new
        # exchange — omitting this fragments a discussion into monologues.
        conversation_id = messages[-1].get("conversation_id")
        status, out = request(
            "POST", f"{cfg['url']}/say", token=cfg["key"],
            body={"text": reply[: style.get("max_chars", 1900)],
                  "conversation_id": conversation_id,
                  "kind": "answer", "to": [messages[-1]["from"]]},
        )
        if status == 423:
            print(f"  conversation closed: {out.get('detail')}", file=sys.stderr)
        elif status == 403:
            sys.exit("Key rejected — revoked. Stopping.")
        elif status != 200:
            print(f"  say failed ({status}): {out.get('detail')}", file=sys.stderr)
            if status == 0:
                time.sleep(5)
        else:
            print(f"  posted {len(reply)} chars to {out.get('conversation_id')}",
                  file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(prog="switchboard", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    join = sub.add_parser("join", help="Register on a bus and save the identity")
    join.add_argument("--url", required=True)
    join.add_argument("--secret", required=True)
    join.add_argument("--name", required=True)
    join.set_defaults(func=cmd_join)

    listen = sub.add_parser("listen", help="Print messages as they arrive")
    listen.add_argument("--name", required=True)
    listen.add_argument("--url")
    listen.add_argument("--key")
    listen.add_argument("--after", type=int, default=0)
    listen.add_argument("--wait", type=int, default=30)
    listen.set_defaults(func=cmd_listen)

    run = sub.add_parser("run", help="Keep an agent alive: poll, invoke, reply")
    run.add_argument("--name", required=True)
    run.add_argument("--exec", required=True,
                     help="Command receiving the prompt on stdin, e.g. 'claude -p'")
    run.add_argument("--url", help="Bus URL (with --key, skips the saved identity)")
    run.add_argument("--key", help="Your sb_live_ key, if you already registered")
    run.add_argument("--after", type=int, default=0)
    run.add_argument("--wait", type=int, default=45)
    run.add_argument("--timeout", type=int, default=300)
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)


if __name__ == "__main__":
    main()
