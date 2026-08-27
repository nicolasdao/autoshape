#!/usr/bin/env python3
"""Congestion sensor that measures what ICMP cannot see.

Packet capture showed 100% of the delay falls between the peer sending and this
machine replying - our outbound response sits in the egress queue behind bulk
upload. A single sparse ICMP echo slips past that queue; a conversation that
must reply repeatedly does not.

So the sensor holds a persistent TCP connection open and times a small
request/response on it. Our request is an outbound packet, so it queues exactly
like the TLS records that were stalling, and the measured time tracks the queue
we actually care about.

Standalone so it can be validated before anything depends on it:
    ./scripts/tcpsense.py --seconds 20
"""
import argparse, socket, statistics as st, sys, threading, time


class Probe:
    """One keep-alive HTTP connection, reconnecting as needed."""

    def __init__(self, host, port=80, path="/generate_204", timeout=8.0):
        self.host, self.port, self.path, self.timeout = host, port, path, timeout
        self.sock = None
        self.req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                    f"Connection: keep-alive\r\nUser-Agent: tcpsense\r\n\r\n").encode()

    def _connect(self):
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # no Nagle delay
        s.settimeout(self.timeout)
        self.sock = s

    def close(self):
        if self.sock:
            try: self.sock.close()
            except Exception: pass
            self.sock = None

    def sample(self):
        """Returns milliseconds, or None on failure."""
        for attempt in (1, 2):
            try:
                if self.sock is None:
                    self._connect()
                # Drain anything left over from a previous response. Without
                # this, leftover bytes satisfy the read immediately and the
                # sample comes back as ~0.1ms - a reading that is not a
                # measurement at all, and one stray value poisons the floor.
                self.sock.setblocking(False)
                try:
                    while self.sock.recv(65536):
                        pass
                except (BlockingIOError, OSError):
                    pass
                self.sock.settimeout(self.timeout)
                t = time.perf_counter()
                self.sock.sendall(self.req)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("closed")
                    buf += chunk
                el = (time.perf_counter() - t) * 1000
                # A server closing keep-alive means the next sample must
                # reconnect; that reconnect cost is not queue delay.
                if b"Connection: close" in buf:
                    self.close()
                return el
            except Exception:
                self.close()
                if attempt == 2:
                    return None
        return None


class Sensor:
    """Runs several probes and exposes a rolling picture of egress delay."""

    def __init__(self, hosts, interval=0.5):
        self.probes = [Probe(h) for h in hosts]
        self.interval = interval
        self.samples = []                  # (monotonic, ms)
        self.lock = threading.Lock()
        self.baseline = None
        self.low = []              # recent samples, for a robust floor estimate
        self._stop = False

    def start(self):
        for p in self.probes:
            threading.Thread(target=self._loop, args=(p,), daemon=True).start()

    def _loop(self, probe):
        while not self._stop:
            ms = probe.sample()
            if ms is not None:
                with self.lock:
                    self.samples.append((time.monotonic(), ms))
                    if len(self.samples) > 400:
                        del self.samples[:-400]
                    # Track the lowest delay ever seen. This is what lets the
                    # sensor bootstrap when it is started while traffic is
                    # ALREADY flowing - the case where there is no idle period
                    # to learn from, and precisely when someone reaches for a
                    # shaper. Without it the baseline stays None, every delta
                    # computes as zero, and the controller is blind.
                    self.low.append(ms)
                    if len(self.low) > 200:
                        del self.low[:-200]
            time.sleep(self.interval)

    def stop(self):
        self._stop = True
        for p in self.probes: p.close()

    def recent(self, window=3.0):
        now = time.monotonic()
        with self.lock:
            return [m for t, m in self.samples if now - t <= window]

    def current(self, window=3.0):
        """Median of the recent window; None until there is data."""
        v = self.recent(window)
        return st.median(v) if v else None

    def learn_idle(self, alpha=0.15):
        """Fold the present reading into the resting baseline. Call ONLY while
        the link is idle, so congestion can never inflate what 'normal' means."""
        c = self.current()
        if c is None: return
        self.baseline = c if self.baseline is None else self.baseline + (c - self.baseline) * alpha

    @property
    def floor(self):
        """Robust estimate of resting delay: the 10th percentile of recent
        samples, not the absolute minimum.

        The minimum is a single point of failure - one spurious near-zero
        reading pins it there forever, every delta is then inflated by the
        link's true resting delay, and the controller cuts far harder than it
        should. Measured live: reference collapsed to 0.0ms and the shaper
        over-cut a 10 Mbps link to 4 Mbps.
        """
        with self.lock:
            if len(self.low) < 5:
                return None
            v = sorted(self.low)
            return v[max(0, int(0.10 * len(v)))]

    def reference(self):
        """Resting delay to compare against.

        Takes the LOWER of the idle-learned baseline and the robust floor,
        which resolves both directions correctly:

          link got faster (resting 100ms -> 20ms, no idle period since)
              baseline is stale at 100, floor tracks down to 20 -> use 20,
              otherwise a stale high baseline hides real congestion forever.
          link is congested (resting 25ms, current 400ms)
              floor rises toward 300 under sustained load, baseline stays 25
              -> use 25, so the congestion is still visible.

        The remaining ambiguous case - the link genuinely slowing with no idle
        period to relearn from - is not decidable from delay alone, and we
        prefer to err toward over-sensitivity rather than blindness.
        """
        vals = [v for v in (self.baseline, self.floor) if v is not None]
        return min(vals) if vals else None

    def excess(self):
        c, ref = self.current(), self.reference()
        if c is None or ref is None: return 0.0
        return c - ref


def main():
    p = argparse.ArgumentParser(description="TCP egress-delay sensor (standalone validation)")
    p.add_argument("--hosts", nargs="+",
                   default=["www.google.com", "www.cloudflare.com", "www.microsoft.com"])
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--seconds", type=float, default=20)
    a = p.parse_args()

    s = Sensor(a.hosts, a.interval)
    s.start()
    print(f"sensing via keep-alive HTTP to {', '.join(a.hosts)}")
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.seconds:
        time.sleep(1)
        v = s.recent(2.0)
        if v:
            print(f"  {time.strftime('%H:%M:%S')}  n={len(v):2d}  "
                  f"median {st.median(v):7.1f} ms   min {min(v):6.1f}   max {max(v):7.1f}")
        else:
            print(f"  {time.strftime('%H:%M:%S')}  no samples yet")
    s.stop()
    allv = [m for _, m in s.samples]
    if allv:
        allv.sort()
        print(f"\n  overall: n={len(allv)}  median {allv[len(allv)//2]:.1f} ms  "
              f"p95 {allv[int(.95*len(allv))]:.1f} ms  max {allv[-1]:.1f} ms")


if __name__ == "__main__":
    main()
