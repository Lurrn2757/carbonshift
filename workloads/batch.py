"""Bounded checksum batch demo: real compute, no network or filesystem writes."""
import argparse
import hashlib
import json
import time


def run(seconds):
    start = time.monotonic()
    iterations = 0
    digest = ""
    payload = b"CarbonShift deterministic checksum workload\n" * 256
    print(json.dumps({"event": "started", "requested_seconds": seconds}), flush=True)
    while time.monotonic() - start < seconds:
        digest = hashlib.sha256(payload).hexdigest()
        iterations += 1
    print(json.dumps({"event": "completed", "iterations": iterations, "last_sha256": digest,
                      "runtime_seconds": time.monotonic() - start}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("seconds must be from 1 to 3600")
    run(args.seconds)
