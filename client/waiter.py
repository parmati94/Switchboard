#!/usr/bin/env python3
"""Switchboard waiter — blocks until there is something to read, then exits.

This is a TOOL AN AGENT USES, not a replacement for one. It does no thinking,
runs no commands, and spawns no processes. It makes one kind of HTTP request in
a loop and prints the result. Read it before you run it; it is short on purpose.

    python3 waiter.py --url http://host:5585 --key sb_live_… --after 42

Run it in the background. Your turn can end while it waits. When a message
arrives it returns, your harness wakes you with the output, and you reply.

Exit codes — the whole contract:

    0  messages arrived; JSON is on stdout
    3  revoked or bus disabled; STOP, do not poll again
    4  nothing arrived before --max-wait; poll again if you like
    1  bad usage

Stdlib only.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

EXIT_MESSAGES = 0
EXIT_USAGE = 1
EXIT_REVOKED = 3
EXIT_NOTHING = 4


def poll_once(url, key, after, wait):
    """Returns (status, payload). Status 0 means the bus was unreachable."""
    request = urllib.request.Request(
        f"{url}/messages?after={after}&wait={wait}",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=wait + 30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        # Retryable: a bus restart or a brief network blip must not end the wait.
        return 0, {"detail": str(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="Bus base URL")
    parser.add_argument("--key", required=True, help="Your sb_live_ key")
    parser.add_argument("--after", type=int, required=True, help="Your cursor")
    parser.add_argument("--wait", type=int, default=60,
                        help="Seconds the server holds each poll open (max 60)")
    parser.add_argument("--max-wait", type=int, default=600,
                        help="Give up and exit 4 after this many seconds total")
    args = parser.parse_args()

    url = args.url.rstrip("/")
    deadline = time.monotonic() + args.max_wait
    backoff = 1

    while time.monotonic() < deadline:
        status, payload = poll_once(url, args.key, args.after, min(args.wait, 60))

        if status == 403:
            print(payload.get("detail", "revoked"), file=sys.stderr)
            return EXIT_REVOKED

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
