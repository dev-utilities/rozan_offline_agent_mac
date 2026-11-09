#!/usr/bin/env python3
"""Chrome native messaging host for the Rozan Bridge extension.

Chrome starts this process when the extension connects. It relays messages between Chrome
(stdin/stdout, 4-byte length-prefixed JSON) and the agent core (a Unix socket, newline-delimited
JSON, mode 600 in the owner's ~/.personal_agent). Standard library only. It holds no logic: every
decision is made by the agent core.
"""
import json
import os
import socket
import struct
import sys
import threading
import time

SOCKET = os.path.join(os.environ.get("AGENT_HOME", os.path.expanduser("~/.personal_agent")), "bridge.sock")
MAX_TO_CHROME = 1024 * 1024          # Chrome's limit for host -> extension messages


def read_chrome(stream):
    head = stream.read(4)
    if len(head) < 4:
        return None
    (n,) = struct.unpack("<I", head)
    return json.loads(stream.read(n).decode("utf-8"))


def write_chrome(stream, obj, lock):
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_TO_CHROME:
        data = json.dumps({"id": obj.get("id"), "cmd": "error", "args": {}}).encode()
    with lock:
        stream.write(struct.pack("<I", len(data)) + data)
        stream.flush()


def main():
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    lock = threading.Lock()
    sock = None
    while sock is None:                       # the agent may start after Chrome
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(SOCKET)
        except OSError:
            sock = None
            time.sleep(2)
    rfile = sock.makefile("rb")

    def from_agent():
        for line in rfile:                    # agent -> Chrome
            write_chrome(stdout, json.loads(line), lock)
        os._exit(0)                           # agent went away; Chrome will reconnect later

    threading.Thread(target=from_agent, daemon=True).start()
    while True:                               # Chrome -> agent
        msg = read_chrome(stdin)
        if msg is None:
            break
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
