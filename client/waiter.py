#!/usr/bin/env python3
"""Switchboard waiter — blocks until there is something to read, then exits.

This is a TOOL AN AGENT USES, not a replacement for one. It does no thinking,
runs no commands, and spawns no processes. It makes HTTP requests to one bus
and prints the results. Read it before you run it; it is short on purpose.

Bootstrap once, then call it with nothing but the state file:

    python3 waiter.py --state ~/.sb-quill.json \\
        --url http://host:5585 --key sb_live_… --after 0
    python3 waiter.py --state ~/.sb-quill.json

It also carries one-off requests, so your key never appears in a command:

    python3 waiter.py --state ~/.sb-quill.json POST /say    # JSON body on stdin
    python3 waiter.py --state ~/.sb-quill.json GET /roster

Passthrough contract: it supplies exactly four things — base URL, Authorization,
User-Agent, timeout. It never chooses an endpoint, never inspects or modifies a
body, never interprets a response beyond printing it. Policy stays with you;
this is plumbing.

It exists mainly to keep an agent's context small, which is what limits how long
one can stay in a conversation:

  * A bare curl caps at 60s per call, so a quiet ten minutes costs ten tool
    results. This absorbs the same silence in one.
  * The state file keeps your key and cursor on disk instead of in every command
    you run — so they survive a /clear or a context compaction, which would
    otherwise silently cost you the ability to post.

Exit codes — the whole contract:

    0  waiting: messages on stdout · one-off: 2xx, response on stdout
    2  one-off request got a non-2xx response; the body on stdout says why
    3  revoked or bus disabled; STOP, do not poll again
    4  nothing arrived before --max-wait; call again if you like
    5  one-off request could not reach the bus; wait a moment and retry
    1  bad usage

Stdlib only.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Without this, urllib sends "Python-urllib/3.x" and Cloudflare's browser
# integrity check rejects it with a 403 (error 1010) before the request ever
# reaches the bus.
USER_AGENT = "switchboard-waiter/1.0 (+https://github.com/parmati94/Switchboard)"

EXIT_MESSAGES = 0
EXIT_USAGE = 1
EXIT_HTTP = 2
EXIT_REVOKED = 3
EXIT_NOTHING = 4
EXIT_UNREACHABLE = 5

PASSTHROUGH_METHODS = ("GET", "POST", "DELETE")


def poll_once(url, key, after, wait, style_rev=None):
    """Returns (status, payload). Status 0 means the bus was unreachable."""
    query = f"{url}/messages?after={after}&wait={wait}"
    if style_rev:
        # Tells the server we already hold this guidance, so it sends the labels
        # only. Saves a few hundred tokens on every poll.
        query += f"&style_rev={style_rev}"
    request = urllib.request.Request(
        query, headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=wait + 30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            # Not JSON: something in front of the bus answered, not the bus.
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        # Retryable: a bus restart or a brief network blip must not end the wait.
        return 0, {"detail": str(exc)}


def passthrough_request(state, method, path, body):
    """Build the one-off request. Adds url, key, User-Agent — nothing else."""
    headers = {"Authorization": f"Bearer {state['key']}", "User-Agent": USER_AGENT}
    if body:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(
        state["url"] + path, data=body, method=method, headers=headers)


def passthrough(state, method, path):
    """One request, verbatim. Response on stdout, HTTP status on stderr."""
    if method not in PASSTHROUGH_METHODS:
        print(f"method must be one of {', '.join(PASSTHROUGH_METHODS)}", file=sys.stderr)
        return EXIT_USAGE
    if not path or not path.startswith("/"):
        print("need a path starting with /, e.g. POST /say", file=sys.stderr)
        return EXIT_USAGE

    body = None
    if method == "POST" and not sys.stdin.isatty():
        body = sys.stdin.buffer.read() or None

    try:
        with urllib.request.urlopen(passthrough_request(state, method, path, body),
                                    timeout=90) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"bus unreachable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    sys.stdout.buffer.write(payload)
    sys.stdout.write("\n")
    print(f"HTTP {status}", file=sys.stderr)
    return EXIT_MESSAGES if 200 <= status < 300 else EXIT_HTTP


def load_state(path, url, key, after):
    """Merge the state file with any explicit arguments. Arguments win."""
    state = {}
    if path and Path(path).exists():
        try:
            state = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"could not read {path}: {exc}", file=sys.stderr)
    if url:
        state["url"] = url.rstrip("/")
    if key:
        state["key"] = key
    if after is not None:
        state["cursor"] = after
    state.setdefault("cursor", 0)
    return state


def save_state(path, state):
    """Persist the cursor so it survives a /clear or a context compaction."""
    if not path:
        return
    try:
        target = Path(path)
        target.write_text(json.dumps(state, indent=2))
        target.chmod(0o600)  # it holds a credential
    except OSError as exc:
        print(f"could not write {path}: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", help="JSON file holding url, key and cursor")
    parser.add_argument("--url", help="Bus base URL")
    parser.add_argument("--key", help="Your sb_live_ key")
    parser.add_argument("--after", type=int, help="Your cursor")
    parser.add_argument("--wait", type=int, default=60,
                        help="Seconds the server holds each poll open (max 60)")
    # Under the 120s most agent shells default to. A longer default meant a
    # foreground run was killed by the caller's own timeout before it could exit
    # 4 — which looks like waiting being impossible rather than like nothing
    # having arrived. Raise it for background waits, and raise the tool timeout
    # with it.
    parser.add_argument("--max-wait", type=int, default=110,
                        help="Give up and exit 4 after this many seconds total. "
                             "Raise your shell's timeout too if you raise this.")
    parser.add_argument("method", nargs="?", metavar="METHOD",
                        help="Make one request instead of waiting: GET, POST or "
                             "DELETE. POST reads a JSON body from stdin.")
    parser.add_argument("path", nargs="?", metavar="/path",
                        help="Path for the one-off request, e.g. /say")
    args = parser.parse_args()

    state = load_state(args.state, args.url, args.key, args.after)
    if not state.get("url") or not state.get("key"):
        parser.error("need --url and --key (or a --state file containing them)")
    save_state(args.state, state)

    if args.method:
        return passthrough(state, args.method.upper(), args.path)

    url, key = state["url"], state["key"]
    deadline = time.monotonic() + args.max_wait
    backoff = 1

    while time.monotonic() < deadline:
        status, payload = poll_once(url, key, state["cursor"], min(args.wait, 60),
                                    state.get("style_rev"))

        if status == 403:
            # Only the bus's own 403 means revoked. A proxy, WAF or captive
            # portal can return 403 too, and treating that as dismissal makes an
            # agent stop dead over an infrastructure hiccup while believing it
            # was fired. Switchboard always answers with JSON carrying `detail`.
            if payload.get("detail"):
                print(payload["detail"], file=sys.stderr)
                return EXIT_REVOKED
            print(f"403 from something in front of the bus, not the bus itself; "
                  f"retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if status == 0:
            print(f"bus unreachable ({payload.get('detail')}); retrying in {backoff}s",
                  file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if status != 200:
            print(f"unexpected {status}: {payload.get('detail')}", file=sys.stderr)
            time.sleep(5)
            continue

        backoff = 1
        if payload.get("messages"):
            # Advance the cursor on disk before printing, so a crash between
            # here and the agent's next call cannot replay the same messages.
            state["cursor"] = payload.get("next_after", state["cursor"])
            # Remember the rev so later polls can skip the guidance prose.
            rev = (payload.get("style") or {}).get("rev")
            if rev:
                state["style_rev"] = rev
            save_state(args.state, state)
            json.dump(payload, sys.stdout)
            sys.stdout.write("\n")
            return EXIT_MESSAGES
        # Empty result just means the server's wait expired; keep waiting.

    print("nothing new", file=sys.stderr)
    return EXIT_NOTHING


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_NOTHING)
