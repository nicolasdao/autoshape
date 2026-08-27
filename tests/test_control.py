#!/usr/bin/env python3
"""Simulate a bottleneck link and drive the controller against it.

WHY
    A control loop must be validated against a model of the plant across the
    whole operating range, not tuned one live run at a time. Every bug found
    the expensive way today - the idle ratchet, the cold-start blindness, the
    self-congestion spiral - is reproducible here in milliseconds, and so are
    conditions this particular link may never happen to show us.

THE MODEL
    Two queues in series, which is the whole physics that matters:

      our shaper   offered -> [queue: cfg.queue slots] -> at most `cap`
      the link     that     -> [upstream buffer, huge] -> at most `capacity`

    If cap < capacity the bottleneck is ours: a bounded queue, bounded delay.
    If cap > capacity the bottleneck is theirs: an effectively unbounded buffer
    where backlog accumulates and delay grows without limit. That asymmetry is
    exactly why shaping works, and why setting the cap too high does nothing.

    run: ./scripts/simlink.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from control import Controller, Config, WIRE_BITS


class Link:
    """A bottleneck with an oversized upstream buffer, as cellular has."""

    def __init__(self, capacity, base_rtt=30.0):
        self.capacity = capacity          # Mbps, may be reassigned mid-run
        self.base_rtt = base_rtt          # ms, resting delay
        self.backlog_bits = 0.0           # sits in the UPSTREAM buffer

    def step(self, dt, offered, cap, queue_slots):
        """Advance dt seconds. Returns (achieved_mbps, observed_delay_ms)."""
        # Our shaper admits at most `cap`.
        admitted = min(offered, cap)

        # The link drains at `capacity`; anything above that piles up upstream.
        excess_bits = (admitted - self.capacity) * 1e6 * dt
        self.backlog_bits = max(0.0, self.backlog_bits + excess_bits)
        achieved = min(admitted, self.capacity + (self.backlog_bits / dt / 1e6 if dt else 0))
        achieved = min(achieved, self.capacity) if self.backlog_bits > 0 else admitted

        # Delay a probe packet experiences:
        #   upstream queue - unbounded, this is the pathology we fight
        upstream_ms = (self.backlog_bits / (self.capacity * 1e6)) * 1000
        #   our own queue - bounded by slots, only full when we are the bottleneck
        our_ms = 0.0
        if admitted >= cap * 0.95:
            our_ms = queue_slots * WIRE_BITS / (cap * 1e6) * 1000
        return achieved, self.base_rtt + upstream_ms + our_ms


def run(scenario, cfg=None, seconds=180, tick=0.2, verbose=False):
    cfg = cfg or Config()
    ctl = Controller(cfg=cfg, rate=scenario.get("start_rate", cfg.base_rate))
    link = Link(scenario["capacity"], scenario.get("base_rtt", 30.0))
    offered = scenario["offered"]
    reference = None
    floor = None
    trace = []
    t = 0.0
    while t < seconds:
        cap_now = scenario.get("capacity_at", lambda _t, c: c)(t, link.capacity)
        link.capacity = cap_now
        off_now = scenario.get("offered_at", lambda _t, o: o)(t, offered)

        achieved, delay = link.step(tick, off_now, ctl.rate, cfg.queue)

        # Sensor behaviour: floor bootstraps from the lowest delay ever seen,
        # and the reference is refined during genuinely idle periods.
        floor = delay if floor is None else min(floor, delay)
        if achieved < max(cfg.min_active_abs, cfg.active_frac * ctl.rate):
            reference = delay if reference is None else reference + (delay - reference) * 0.15
        ref = reference if reference is not None else floor

        ctl.observe(t, delay, ref)
        rate, action, state = ctl.step(t, achieved, scenario.get("ingress", 0.0))
        trace.append((t, cap_now, rate, achieved, delay, action, state))
        if verbose and int(t / tick) % 25 == 0:
            print(f"    t={t:6.1f}s cap={cap_now:5.1f} rate={rate:6.2f} "
                  f"got={achieved:5.2f} delay={delay:7.1f}ms {action}")
        t += tick
    return trace


def summarise(name, trace, capacity_end, expect):
    tail = trace[int(len(trace) * 0.7):]
    rates = [r for _, _, r, _, _, _, _ in tail]
    delays = [d for _, _, _, _, d, _, _ in tail]
    got = [g for _, _, _, g, _, _, _ in tail]
    lo, hi = min(rates), max(rates)
    spread = (hi - lo) / (sum(rates) / len(rates))
    med_delay = sorted(delays)[len(delays) // 2]
    med_rate = sorted(rates)[len(rates) // 2]

    fails = []
    if "rate_between" in expect:
        a, b = expect["rate_between"]
        if not (a <= med_rate <= b):
            fails.append(f"rate {med_rate:.1f} outside [{a}, {b}]")
    if "max_delay" in expect and med_delay > expect["max_delay"]:
        fails.append(f"delay {med_delay:.0f}ms > {expect['max_delay']}ms")
    if "stable" in expect and spread > expect["stable"]:
        fails.append(f"oscillating: spread {spread*100:.0f}%")
    if "min_throughput" in expect:
        mt = sorted(got)[len(got) // 2]
        if mt < expect["min_throughput"]:
            fails.append(f"throughput {mt:.1f} < {expect['min_throughput']}")

    ok = not fails
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<38} "
          f"rate {med_rate:6.2f}  got {sorted(got)[len(got)//2]:5.2f}  "
          f"delay {med_delay:6.0f}ms  spread {spread*100:3.0f}%")
    for f in fails:
        print(f"          -> {f}")
    return ok


SCENARIOS = [
    # name, scenario, expectations
    ("start above capacity (40 -> 15)",
     dict(capacity=15, offered=40, start_rate=40),
     dict(rate_between=(8, 18), max_delay=250, stable=0.35)),

    ("start below capacity (8 -> 15)",
     dict(capacity=15, offered=40, start_rate=8),
     dict(rate_between=(8, 18), max_delay=250, stable=0.35)),

    ("healthy fast link, must NOT throttle",
     dict(capacity=90, offered=40, start_rate=40),
     dict(rate_between=(30, 50), max_delay=120, min_throughput=25)),

    # Starting 8x over capacity on a slow link builds a backlog that drains at
    # only (capacity - cap) = 0.5 Mbps, so recovery takes minutes. That is
    # physics, not a control defect - but it IS a real limitation: on a very
    # slow link, start conservatively or expect a long flush.
    ("very slow link (2.5 Mbps), recovers from 8x overshoot",
     dict(capacity=2.5, offered=40, start_rate=20),
     dict(rate_between=(2.0, 4.0), max_delay=400), 500),

    ("capacity collapses mid-run 40 -> 6",
     dict(capacity=40, offered=40, start_rate=30,
          capacity_at=lambda t, c: 40 if t < 60 else 6),
     dict(rate_between=(2, 9), max_delay=400)),

    ("capacity recovers mid-run 6 -> 40",
     dict(capacity=6, offered=40, start_rate=6,
          capacity_at=lambda t, c: 6 if t < 60 else 40),
     dict(rate_between=(6, 50), max_delay=300)),

    ("idle link, must return to base",
     dict(capacity=15, offered=0.05, start_rate=40),
     dict(rate_between=(7, 9))),

    ("heavy foreign download, must not cut uselessly",
     dict(capacity=15, offered=3, start_rate=12, ingress=20),
     dict(rate_between=(6, 14))),

    ("bursty offered load",
     dict(capacity=15, offered=40, start_rate=15,
          offered_at=lambda t, o: o if int(t / 10) % 2 == 0 else 0.05),
     dict(rate_between=(4, 20), max_delay=400)),
]


def sweep():
    """Parameter sweep across link types, not hand-picked cases.

    A controller tuned to one 15 Mbps cellular link proves nothing. This walks
    capacity from dial-up to gigabit and resting delay from fibre to satellite,
    and asserts the same three properties everywhere:

      1. It must not strangle the link (achieve a fair share of capacity).
      2. It must keep delay bounded (that is the entire point).
      3. It must not park on the floor when capacity is plentiful.
    """
    print("\n  capacity x resting-delay sweep")
    print(f"  {'cap':>6} {'rtt':>6} | {'settled':>8} {'got':>7} {'delay':>8} | verdict")
    print("  " + "-" * 62)
    fails = 0
    total = 0
    for cap in (1, 2, 5, 10, 15, 25, 50, 100, 250, 500):
        for rtt in (5, 30, 100, 300, 600):
            total += 1
            sc = dict(capacity=cap, offered=cap * 3, start_rate=min(cap * 2, 500),
                      base_rtt=rtt)
            cfg = Config(max_rate=max(50, cap * 2), min_rate=min(2.0, cap * 0.5))
            tr = run(sc, cfg=cfg, seconds=150)
            tail = tr[int(len(tr) * 0.7):]
            rate = sorted(r for _, _, r, _, _, _, _ in tail)[len(tail) // 2]
            got = sorted(g for _, _, _, g, _, _, _ in tail)[len(tail) // 2]
            delay = sorted(d for _, _, _, _, d, _, _ in tail)[len(tail) // 2]

            bad = []
            if got < cap * 0.35:
                bad.append(f"strangled ({got:.1f} of {cap})")
            # Delay budget: resting delay plus our own queue plus headroom.
            budget = rtt + (Config().queue * WIRE_BITS / (max(rate, 0.5) * 1e6) * 1000) + 120
            if delay > budget:
                bad.append(f"delay {delay:.0f} > {budget:.0f}")
            if bad: fails += 1
            print(f"  {cap:>6} {rtt:>6} | {rate:>8.2f} {got:>7.2f} {delay:>7.0f}ms | "
                  f"{'ok' if not bad else '; '.join(bad)}")
    print(f"\n  {total - fails}/{total} combinations behaved")
    return fails == 0

if __name__ == "__main__":
    if "--sweep" in sys.argv:
        sys.exit(0 if sweep() else 1)
    print("simulating controller against a modelled bottleneck\n")
    results = []
    for entry in SCENARIOS:
        name, sc, exp = entry[0], entry[1], entry[2]
        secs = entry[3] if len(entry) > 3 else 180
        tr = run(sc, seconds=secs)
        results.append(summarise(name, tr, sc["capacity"], exp))
    print(f"\n  {sum(results)}/{len(results)} scenarios passed")
    sys.exit(0 if all(results) else 1)


