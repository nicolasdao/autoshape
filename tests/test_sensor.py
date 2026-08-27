#!/usr/bin/env python3
"""Self-test for the congestion sensor. No root, no shaping, ~40s.

The simulator validates the CONTROL LOGIC but stubs the sensor, so sensor bugs
sail straight through it. Both failures found the expensive way - the reference
collapsing to zero from one spurious near-zero sample, and the cold-start
blindness when no idle period exists - lived in exactly that gap.

Asserts the four properties the controller depends on:
  1. a plausible resting reference (not zero, not absurd)
  2. a large, unambiguous rise under load
  3. recovery afterwards
  4. no impossible samples (sub-millisecond "measurements")
"""
import subprocess, sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tcpsense import Sensor

HOSTS = ["www.google.com", "www.cloudflare.com", "www.microsoft.com"]
LOAD = ("while :; do head -c 20000000 /dev/urandom | curl -s -o /dev/null --max-time 40 "
        "-X POST --data-binary @- https://speed.cloudflare.com/__up 2>/dev/null; done")

def main():
    s = Sensor(HOSTS, 0.5); s.start()
    fails = []
    time.sleep(8)
    ref_idle, cur_idle = s.reference(), s.current()
    print(f"  idle   reference {ref_idle if ref_idle else 0:6.1f} ms   current {cur_idle if cur_idle else 0:6.1f} ms")
    if not ref_idle or ref_idle < 1:
        fails.append(f"reference implausible at idle ({ref_idle}) - the zero-collapse bug")
    if not cur_idle or cur_idle > 400:
        fails.append(f"idle latency {cur_idle} - link not quiet, cannot judge")

    procs = [subprocess.Popen(LOAD, shell=True) for _ in range(8)]
    time.sleep(16)
    ref_load, cur_load = s.reference(), s.current()
    print(f"  load   reference {ref_load if ref_load else 0:6.1f} ms   current {cur_load if cur_load else 0:6.1f} ms"
          f"   excess {s.excess():+7.1f} ms")
    for p in procs: p.terminate()
    subprocess.run(["pkill", "-f", "speed.cloudflare.com/__up"], capture_output=True)

    if cur_load and cur_idle and cur_load < cur_idle * 3:
        fails.append(f"no clear rise under load ({cur_idle:.0f} -> {cur_load:.0f}) - "
                     f"either the sensor is blind, or the link genuinely coped")
    if ref_load and ref_idle and ref_load > ref_idle * 3:
        fails.append(f"reference inflated under load ({ref_idle:.0f} -> {ref_load:.0f}) - "
                     f"congestion is poisoning the baseline")

    time.sleep(8)
    cur_after = s.current()
    print(f"  after  reference {s.reference() or 0:6.1f} ms   current {cur_after if cur_after else 0:6.1f} ms")
    if cur_after and cur_idle and cur_after > cur_idle * 3:
        fails.append(f"did not recover ({cur_after:.0f} vs idle {cur_idle:.0f})")

    lows = [m for _, m in s.samples if m < 1.0]
    print(f"  impossible samples (<1ms): {len(lows)}")
    if lows:
        fails.append(f"{len(lows)} sub-millisecond samples - stale socket bytes, poisons the floor")
    s.stop()

    print()
    for f in fails: print(f"  FAIL  {f}")
    if not fails: print("  PASS  sensor behaves correctly")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
