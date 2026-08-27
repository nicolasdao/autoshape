#!/usr/bin/env python3
"""Pure control logic for the adaptive shaper - no I/O, no clock, no network.

Separated so it can be driven by a simulator across hundreds of scenarios in
seconds, instead of being validated one 2-minute live run at a time. Every
input is passed in; the caller owns time, measurement and actuation.
"""

from dataclasses import dataclass, field

WIRE_BITS = 1500 * 8 + 240      # MTU plus framing


@dataclass
class Config:
    base_rate: float = 8.0
    min_rate: float = 2.0
    max_rate: float = 50.0
    queue: int = 16
    threshold: float = 60.0      # ms over resting delay counting as delayed
    detect_window: int = 8
    detect_thr: int = 3
    sample_window: float = 2.0   # seconds of samples considered
    alpha_delta: float = 0.095
    load_gate: float = 0.75
    # "Are we meaningfully using our own cap?" must be RELATIVE, not absolute.
    # An absolute 1 Mbps floor silently broke every link slower than that: on a
    # 1 Mbps link achieved throughput never reached it, so the controller
    # believed it was idle while drowning, learned its congested delay as the
    # resting baseline, and went permanently blind.
    active_frac: float = 0.30    # fraction of the current cap that counts as in use
    min_active_abs: float = 0.05 # Mbps, only to reject measurement noise
    down_min: float = 0.99       # gentlest cut
    down_max: float = 0.80       # harshest cut
    max_down_delta: float = 200.0
    achieved_floor: float = 0.85 # never cut below this fraction of the throughput we
                                 # are actually achieving - the link demonstrably
                                 # carries that much, so going lower discards
                                 # capacity we have already proven exists
    up_factor: float = 0.02
    down_refractory: float = 0.3
    up_refractory: float = 1.0
    idle_decay: float = 0.05
    # Whether foreign inbound traffic makes delay unattributable must be
    # RELATIVE to what we are sending, not an absolute Mbps. At an absolute
    # 3 Mbps, any link where ordinary browsing exceeds that would permanently
    # refuse to cut - the controller silently disabled on fast links. What
    # actually matters is whether their download dominates our upload.
    blind_ratio: float = 2.0     # foreign inbound this many times our egress
    blind_abs: float = 1.0       # Mbps floor, to ignore trickles


@dataclass
class Controller:
    cfg: Config = field(default_factory=Config)
    rate: float = None
    samples: list = field(default_factory=list)   # (t, delta_ms, delayed)
    avg_delta: float = 0.0
    last_down: float = -1e9
    last_up: float = -1e9

    def __post_init__(self):
        if self.rate is None:
            self.rate = self.cfg.base_rate

    # -- the self-congestion correction -----------------------------------
    def own_queue_ms(self):
        """Delay OUR OWN pipe adds when its queue is full.

        A probe packet can sit behind the entire queue, so this is
        queue-depth x per-packet time, not one packet. Compensating for a single
        packet was a measured death-spiral: at a 2 Mbps cap a full 16-slot queue
        is 98ms, the controller read its own buffer as congestion, cut, made its
        own queue slower, and ratcheted to the floor with 210 consecutive cuts.
        """
        return self.cfg.queue * WIRE_BITS / max(1.0, self.rate * 1000)

    def threshold(self):
        return self.cfg.threshold + self.own_queue_ms()

    # -- congestion decision ----------------------------------------------
    def observe(self, t, delay_ms, reference_ms):
        """Record one delay reading. reference_ms is the link's resting delay;
        None means we have no reference yet and must not fabricate a delta."""
        if reference_ms is None:
            return
        d = delay_ms - reference_ms
        self.samples.append((t, d, d > self.threshold()))
        cutoff = t - self.cfg.sample_window
        self.samples = [s for s in self.samples if s[0] >= cutoff][-200:]

    def congested(self):
        """N-of-M voting. Never decide on one reading: jitter throws isolated
        spikes constantly, while a filling buffer produces runs of them."""
        win = self.samples[-self.cfg.detect_window:]
        if len(win) < self.cfg.detect_thr:
            return False, 0, len(win)
        n = sum(1 for _, _, d in win if d)
        return n >= self.cfg.detect_thr, n, len(win)

    def _already_improving(self):
        """Is delay currently FALLING?

        This is the single most important guard, and the one whose absence
        caused every runaway. Cutting the rate does not clear the upstream
        backlog instantly - the backlog has to drain at the link rate, which
        takes seconds. During that drain, delay is still high even though the
        rate is already correct.

        A controller that keeps cutting through the drain is reacting to a
        problem it has already solved. In simulation that walked a 15 Mbps link
        from 40 Mbps to the 2 Mbps floor.

        So the rule is not "has delay improved by X%" - during a slow drain it
        may not, within any one refractory period. The rule is simply: if the
        recent half of our sample window is lower than the older half, the
        correction is working. Do nothing and let it work.
        """
        if len(self.samples) < 6:
            return False
        half = len(self.samples) // 2
        older = sorted(d for _, d, _ in self.samples[:half])
        newer = sorted(d for _, d, _ in self.samples[half:])
        return newer[len(newer) // 2] < older[len(older) // 2]

    def median_delta(self):
        if not self.samples:
            return 0.0
        v = sorted(d for _, d, _ in self.samples)
        return v[len(v) // 2]

    # -- one control step --------------------------------------------------
    def step(self, t, egress, ingress):
        """Returns (new_rate, action, state). Pure: no side effects."""
        c = self.cfg
        load = egress / self.rate if self.rate > 0 else 0.0
        bloat, delayed, win = self.congested()

        # The jitter estimate must be learned OFF-load. Learn it while congested
        # and "normal" inflates to include the congestion, blinding the shaper.
        if load < c.load_gate:
            self.avg_delta += (self.median_delta() - self.avg_delta) * c.alpha_delta

        active = egress >= max(c.min_active_abs, c.active_frac * self.rate)
        # A heavy download raises delay too, and we cannot shape someone else's
        # inbound stream. Subtract our own ACK return traffic before judging.
        foreign_down = max(0.0, ingress - egress * 0.035)
        blind = foreign_down > max(c.blind_abs, c.blind_ratio * egress)

        action, state = "hold", "clear"

        if not active:
            # Idle carries no information about our own impact; returning to a
            # known-safe base beats holding a stale extreme.
            state = "idle"
            if abs(self.rate - c.base_rate) > 0.1:
                self.rate += (c.base_rate - self.rate) * c.idle_decay
                action = "decay"

        elif bloat and blind:
            state = "down-traffic"

        elif (bloat and t - self.last_down >= c.down_refractory
              and not self._already_improving()):
            d = self.median_delta()
            span = max(1.0, c.max_down_delta - self.threshold())
            frac = min(1.0, max(0.0, (d - self.threshold()) / span))
            proposed = self.rate * (c.down_min - (c.down_min - c.down_max) * frac)
            # Bound the descent by measured reality. Without this the cut is
            # unbounded: the controller keeps cutting while the upstream backlog
            # drains, and in simulation walked a 15 Mbps link from 40 Mbps down
            # to the 2 Mbps floor - discarding 13 Mbps it had just demonstrated.
            self.rate = max(proposed, egress * c.achieved_floor)
            self.last_down = t
            state, action = "congested", "DOWN"

        elif (not bloat and load >= c.load_gate
              and t - self.last_up >= c.up_refractory
              and t - self.last_down >= c.down_refractory):
            # Taper the rise as delay approaches the trigger, so we stop just
            # short of causing the problem rather than overshooting into it.
            head = max(0.0, 1.0 - self.avg_delta / max(1.0, c.threshold))
            self.rate *= 1 + c.up_factor * head
            self.last_up = t
            state, action = "loaded", "up"

        self.rate = max(c.min_rate, min(c.max_rate, self.rate))
        return self.rate, action, state
