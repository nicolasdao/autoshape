#!/usr/bin/env python3
"""Adaptive egress shaper for a link whose capacity moves underneath you.

WHY THIS EXISTS
    A fixed cap cannot work on cellular. This hotspot measured 6.2, 8.9, 15.6,
    28.7 and 90.2 Mbps uplink across one afternoon. A cap tuned at 6 throttles
    you pointlessly at 90; a cap tuned at 90 protects nothing at 6.

THE IDEA
    Never measure bandwidth. Measure *delay*, and infer congestion from it.
    When the buffer in the hotspot's cellular radio starts filling, round-trip
    time rises above its floor well before throughput suffers.

        delay rising while we are sending -> our packets are queueing -> DOWN
        delay flat AND we are using the pipe hard                     -> UP
        link idle                                        -> drift toward base

    Capping our own egress below the true uplink rate moves the bottleneck onto
    this machine, where the queue is 16 slots deep instead of seconds deep.

DESIGN NOTES
    Structure follows cake-autorate, the reference implementation for this
    technique. The non-obvious parts are the ones that matter, and each is
    commented where it appears:

      * Serialization compensation - at a low cap a single MTU packet is itself
        worth milliseconds, so an uncompensated shaper reads its own
        transmissions as congestion and death-spirals to the floor.
      * Asymmetric baseline - falls almost instantly toward a new low, rises
        ~900x slower, so sustained load cannot drag the baseline up to meet the
        congestion it is meant to reveal.
      * N-of-M voting over a sample window, never one reading. Radio jitter
        throws isolated spikes constantly; a filling buffer produces runs.
      * Proportional adjustment - a small excursion earns a small cut.
      * The load gate - rate only climbs under demonstrated demand, else it
        drifts to maximum and is wide open exactly when traffic arrives.

RUN
    sudo ./scripts/autoshape.py --verbose
    sudo ./scripts/autoshape.py --dry-run --verbose    # decide, touch nothing

    Ctrl-C removes all shaping on the way out.
"""

import argparse
import collections
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tcpsense import Sensor
from control import Controller, Config

REPO = "nicolasdao/autoshape"
# GitHub defaults to `main` for new repos but plenty still use `master`, so try
# both rather than silently failing every update check.
BRANCHES = ("main", "master")
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
CHECK_EVERY = 86400           # seconds between update checks - once a day, at most

PIPE = 2                      # must match shape.sh so the two never fight
ANCHOR = "com.apple/shape"
WIRE_BITS = 1500 * 8 + 240    # MTU plus framing, for serialization compensation


# NOTE: this controller originally sensed congestion with ICMP. That was wrong,
# and measurably so: under load ICMP showed +14ms delay and 0% loss across 400
# ticks while real HTTPS requests took 2672ms. Packet capture then showed why -
# 100% of the delay sits between the peer sending and this machine replying, so
# our outbound response is stuck in the egress queue. A single sparse ping slips
# past that queue; a conversation that must reply repeatedly does not.
#
# The sensor is now a keep-alive TCP probe (tcpsense.Sensor), validated against
# deliberately induced bufferbloat before being wired in here:
#     idle 30.2ms -> loaded 542.9ms -> induced bloat 289.8ms -> recovered 30.6ms
# while ICMP stayed flat throughout.


# ---------------------------------------------------------------------------
# live status display
# ---------------------------------------------------------------------------

C = dict(reset="\033[0m", dim="\033[2m", bold="\033[1m",
         green="\033[32m", yellow="\033[33m", red="\033[31m",
         cyan="\033[36m", grey="\033[90m")


def _plain():
    return {k: "" for k in C}


def bar(frac, width=14, colour=""):
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return colour + "\u2588" * n + C["grey"] + "\u2591" * (width - n) + C["reset"]


class Display:
    """Compact live panel. Redraws in place; degrades to plain lines when not
    attached to a terminal (piped to a file, run from a script)."""

    LINES = 8

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.c = C if self.tty else _plain()
        self.drawn = False
        self.peak_delay = 0.0

    def header(self, iface, lo, hi):
        c = self.c
        print(f"{c['bold']}  autoshape{c['reset']}  {c['dim']}adaptive uplink guard on "
              f"{iface} \u00b7 limits {lo:.0f}-{hi:.0f} Mbps{c['reset']}")
        print(f"  {c['dim']}press Ctrl-C to stop and remove all shaping{c['reset']}")
        print()

    def update(self, rate, used, delay, resting, threshold, state, action, lo, hi):
        c = self.c
        excess = (delay - resting) if (delay is not None and resting is not None) else 0.0
        self.peak_delay = max(self.peak_delay, delay or 0)

        # Status is about the user's experience, not the controller's internals.
        if delay is None:
            word, col, note = "STARTING", c["dim"], "measuring your connection"
        elif excess >= threshold:
            word, col, note = "CONGESTED", c["red"], "easing off to clear the queue"
        elif excess >= threshold * 0.5:
            word, col, note = "BUSY", c["yellow"], "watching closely"
        else:
            word, col, note = "GOOD", c["green"], "internet is responsive"

        dcol = c["green"] if excess < threshold * 0.5 else (c["yellow"] if excess < threshold else c["red"])
        load = (used / rate) if rate else 0
        limiting = "limiting" if load > 0.85 else "headroom"
        arrow = {"DOWN": c["red"] + "\u25bc lowering", "up": c["green"] + "\u25b2 raising",
                 "decay": c["dim"] + "\u2192 easing back", "hold": c["dim"] + "steady"}.get(action, c["dim"] + "steady")

        rows = [
            f"  {c['bold']}{col}{word:<10}{c['reset']} {c['dim']}{note}{c['reset']}",
            "",
            f"  {c['dim']}speed limit{c['reset']}   {c['bold']}{rate:6.1f}{c['reset']} Mbps  "
            f"{bar((rate - lo) / max(1e-9, hi - lo), 14, c['cyan'])}  {arrow}{c['reset']}",
            f"  {c['dim']}in use     {c['reset']}   {used:6.1f} Mbps  "
            f"{bar(load, 14, c['cyan'])}  {c['dim']}{load*100:3.0f}% \u00b7 {limiting}{c['reset']}",
            "",
            f"  {c['dim']}delay      {c['reset']}   {dcol}{(delay or 0):6.0f}{c['reset']} ms    "
            f"{bar(excess / max(1.0, threshold * 1.5), 14, dcol)}  "
            f"{c['dim']}+{excess:.0f} over {(resting or 0):.0f} resting{c['reset']}",
            f"  {c['dim']}worst seen {c['reset']}   {self.peak_delay:6.0f} ms",
        ]
        if self.tty:
            if self.drawn:
                sys.stdout.write(f"\033[{self.LINES - 1}A")
            for r in rows:
                sys.stdout.write("\033[K" + r + "\n")
            sys.stdout.flush()
            self.drawn = True
        else:
            print(f"  {word:<10} cap {rate:6.1f} Mbps  used {used:6.1f} ({load*100:3.0f}%)  "
                  f"delay {(delay or 0):5.0f}ms (+{excess:.0f})  {action}")



# ---------------------------------------------------------------------------
# version + self-update
# ---------------------------------------------------------------------------

def version():
    """Read VERSION from alongside this file. One source of truth, so the
    installed copy and the repo cannot drift apart."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _fetch(path, timeout=4):
    """Fetch a repo file, trying each candidate branch."""
    import urllib.request
    last = None
    for br in BRANCHES:
        url = f"https://raw.githubusercontent.com/{REPO}/{br}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode().strip()
        except Exception as e:
            last = e
    raise last


def latest_version():
    try:
        return _fetch("VERSION")
    except Exception:
        return None


def check_for_update():
    """Tell the user once a day if a newer version exists. Never blocks start-up,
    never fails loudly, and can be turned off entirely.

    Cached so it contacts GitHub at most once per day rather than on every run -
    a tool that phones home constantly is a tool people stop trusting.
    """
    if os.environ.get("AUTOSHAPE_NO_UPDATE_CHECK"):
        return
    stamp = "/tmp/.autoshape-update-check"
    try:
        if os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < CHECK_EVERY:
            return
    except OSError:
        pass
    latest = latest_version()
    try:
        open(stamp, "w").close()
    except OSError:
        pass
    if latest and latest != version():
        print(f"  {C['yellow']}update available: {version()} -> {latest}{C['reset']}")
        print(f"  {C['dim']}run: sudo autoshape --update{C['reset']}\n")


def self_update():
    """Re-run the official installer, which fetches the current files."""
    import urllib.request, subprocess, tempfile
    cur, latest = version(), latest_version()
    if latest is None:
        print("  could not reach GitHub. check your connection and try again.")
        return 1
    if latest == cur:
        print(f"  already up to date ({cur}).")
        return 0
    print(f"  updating {cur} -> {latest} ...")
    try:
        script = _fetch("install.sh", timeout=15)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script)
            path = f.name
        r = subprocess.run(["sh", path])
        os.unlink(path)
        if r.returncode == 0:
            print(f"  updated to {latest}.")
        return r.returncode
    except Exception as e:
        print(f"  update failed: {e}")
        print(f"  you can always reinstall manually:")
        print(f"    curl -fsSL {RAW}/install.sh | sh")
        return 1


class Shaper:
    def __init__(self, a):
        self.a = a
        self.rate = a.base_rate
        self.iface = self._phys_iface()
        self.samples = collections.deque(maxlen=400)   # merged across reflectors
        self.avg_delta = 0.0            # jitter estimate, learned OFF-load only
        self.last_down = 0.0
        self.last_up = 0.0
        self.last_in, self.last_bytes = self._iobytes()
        self.last_ts = time.monotonic()
        self.rate_fresh = False         # a new throughput sample since last raise
        self.installed = False
        self.log = None

    # ---- plumbing --------------------------------------------------------

    def _phys_iface(self):
        out = subprocess.run(["netstat", "-rn", "-f", "inet"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 2 and f[0] == "default" and f[-1].startswith("en"):
                return f[-1]
        sys.exit("no physical default route (en*) - are you on Wi-Fi?")

    def _iobytes(self):
        out = subprocess.run(["netstat", "-ibn", "-I", self.iface], capture_output=True, text=True).stdout
        for line in out.splitlines():
            f = line.split()
            if len(f) > 9 and "Link" in f[2]:
                return int(f[6]), int(f[9])
        return 0, 0

    def _obytes(self):
        return self._iobytes()[1]

    def _lan(self):
        ip = subprocess.run(["ipconfig", "getifaddr", self.iface],
                            capture_output=True, text=True).stdout.strip()
        p = ip.split(".")
        return f"{p[0]}.{p[1]}.{p[2]}.0/24" if len(p) == 4 else None

    def install(self):
        if self.a.dry_run:
            self.installed = True; return
        self.apply(self.rate, force=True)
        lan = self._lan()
        rules = ["no dummynet quick on lo0 all"]
        if lan: rules.append(f"no dummynet quick on {self.iface} from any to {lan}")
        rules.append(f"dummynet out quick on {self.iface} from any to any pipe {PIPE}")
        subprocess.run(["pfctl", "-a", ANCHOR, "-f", "-"], input="\n".join(rules) + "\n",
                       text=True, capture_output=True)
        subprocess.run(["pfctl", "-E"], capture_output=True, text=True)
        self.installed = True

    def apply(self, mbps, force=False):
        mbps = max(self.a.min_rate, min(self.a.max_rate, mbps))
        if not force and abs(mbps - self.rate) < 0.02:
            self.rate = mbps; return
        self.rate = mbps
        if self.a.dry_run: return
        subprocess.run(["dnctl", "pipe", str(PIPE), "config", "bw", f"{int(mbps*1000)}Kbit/s",
                        "queue", str(self.a.queue)], capture_output=True)

    def remove(self):
        if not self.installed or self.a.dry_run: return
        subprocess.run(["pfctl", "-a", ANCHOR, "-F", "all"], capture_output=True)
        subprocess.run(["dnctl", "-q", "pipe", "delete", str(PIPE)], capture_output=True)
        subprocess.run(["pfctl", "-X", "1"], capture_output=True)

    # ---- signals ---------------------------------------------------------

    def rates(self):
        """(egress_mbps, ingress_mbps). Ingress matters because we can only
        shape what we send."""
        now = time.monotonic()
        i, o = self._iobytes()
        el = now - self.last_ts
        if el <= 0:
            return 0.0, 0.0
        up = max(0.0, (o - self.last_bytes) * 8 / el / 1e6)
        down = max(0.0, (i - self.last_in) * 8 / el / 1e6)
        self.last_bytes, self.last_in, self.last_ts = o, i, now
        return up, down

    # ---- control loop ----------------------------------------------------

    def run(self):
        a = self.a
        cfg = Config(base_rate=a.base_rate, min_rate=a.min_rate, max_rate=a.max_rate,
                     queue=a.queue, threshold=a.threshold, detect_window=a.detect_window,
                     detect_thr=a.detect_thr, sample_window=a.window,
                     alpha_delta=a.alpha_delta, load_gate=a.load_gate,
                     active_frac=a.active_frac, down_min=a.down_min, down_max=a.down_max,
                     max_down_delta=a.max_down_delta, up_factor=a.up_factor,
                     down_refractory=a.down_refractory, up_refractory=a.up_refractory,
                     idle_decay=a.idle_decay, blind_ratio=a.blind_ratio)
        ctl = Controller(cfg=cfg, rate=a.base_rate)
        sensor = Sensor(a.probe_hosts, a.probe_interval)
        sensor.start()

        if a.log:
            os.makedirs(os.path.dirname(a.log) or ".", exist_ok=True)
            self.log = open(a.log, "w")
            self.log.write("ts,rate_mbps,egress_mbps,ingress_mbps,delay_ms,ref_ms,"
                           "thresh_ms,state,action\n")

        disp = Display()
        disp.header(self.iface, a.min_rate, a.max_rate)
        if a.dry_run:
            print("  DRY RUN - deciding but not touching dnctl\n")

        self.install()
        time.sleep(a.warmup)
        t0 = time.monotonic()

        while True:
            time.sleep(a.tick)
            t = time.monotonic() - t0
            eg, ing = self.rates()

            ref = sensor.reference()
            for ms in sensor.recent(a.tick * 1.5):
                ctl.observe(t, ms, ref)

            if eg < max(cfg.min_active_abs, cfg.active_frac * ctl.rate):
                sensor.learn_idle()

            rate, action, state = ctl.step(t, eg, ing)
            self.apply(rate)

            cur = sensor.current()
            if self.log:
                self.log.write(f"{time.strftime('%H:%M:%S')},{rate:.2f},{eg:.2f},{ing:.2f},"
                               f"{cur if cur is not None else ''},"
                               f"{ref if ref is not None else ''},"
                               f"{ctl.threshold():.1f},{state},{action}\n")
                self.log.flush()
            disp.update(rate, eg, cur, ref, ctl.threshold(), state, action,
                        a.min_rate, a.max_rate)


def main():
    p = argparse.ArgumentParser(description="Latency-driven adaptive egress shaper")
    g = p.add_argument_group("rates (Mbit/s)")
    g.add_argument("--base-rate", type=float, default=8.0, help="safe resting cap, returned to when idle")
    g.add_argument("--min-rate", type=float, default=2.0)
    g.add_argument("--max-rate", type=float, default=50.0,
                   help="set slightly BELOW true peak so traffic cruises instead of probing past it")
    g.add_argument("--queue", type=int, default=16, help="pipe queue in slots (measured knee)")

    d = p.add_argument_group("congestion detection")
    d.add_argument("--threshold", type=float, default=60.0,
                   help="ms over resting delay counting as delayed. Measured on this link: "
                        "resting ~30ms, congested 290-540ms, so 60 sits clear of noise "
                        "while catching the fault early")
    d.add_argument("--detect-window", type=int, default=8, help="samples examined")
    d.add_argument("--detect-thr", type=int, default=3, help="delayed samples needed to call it")
    d.add_argument("--window", type=float, default=2.0, help="age limit on samples, s")
    d.add_argument("--alpha-delta", type=float, default=0.095)

    c = p.add_argument_group("control")
    c.add_argument("--tick", type=float, default=0.2)
    c.add_argument("--load-gate", type=float, default=0.75)
    c.add_argument("--active-frac", type=float, default=0.30,
                   help="fraction of the current cap that counts as 'in use'; relative "
                        "so it works on a 1 Mbps link and a 1 Gbps one alike")
    c.add_argument("--down-min", type=float, default=0.99, help="gentlest cut")
    c.add_argument("--down-max", type=float, default=0.80, help="harshest cut")
    c.add_argument("--max-down-delta", type=float, default=200.0,
                   help="ms of excess earning the harshest cut")
    c.add_argument("--up-factor", type=float, default=0.02, help="max fractional raise per step")
    c.add_argument("--down-refractory", type=float, default=0.3)
    c.add_argument("--up-refractory", type=float, default=1.0)
    c.add_argument("--idle-decay", type=float, default=0.05)
    c.add_argument("--stall-timeout", type=float, default=10.0)
    c.add_argument("--blind-ratio", type=float, default=2.0,
                   help="foreign inbound this many times our egress means delay cannot be "
                        "attributed to us, so we hold rather than cut uselessly")
    c.add_argument("--warmup", type=float, default=5.0)

    p.add_argument("--probe-hosts", nargs="+",
                   default=["www.google.com", "www.cloudflare.com", "www.microsoft.com"])
    p.add_argument("--probe-interval", type=float, default=0.5)
    p.add_argument("--log", default="captures/autoshape.csv")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--off", action="store_true",
                   help="remove any shaping this tool left behind, and exit")
    p.add_argument("--version", action="store_true", help="print version and exit")
    p.add_argument("--update", action="store_true", help="update to the latest release")
    p.add_argument("--no-update-check", action="store_true",
                   help="never check GitHub for a newer version")
    a = p.parse_args()

    if a.version:
        print(f"autoshape {version()}")
        return
    if a.update:
        sys.exit(self_update())

    if a.off:
        if os.geteuid() != 0:
            sys.exit("needs root: sudo ./autoshape.py --off")
        sh = Shaper(a); sh.installed = True; sh.remove()
        print("  all shaping removed. your connection is unrestricted.")
        return

    if os.geteuid() != 0 and not a.dry_run:
        sys.exit("needs root: sudo ./scripts/autoshape.py")

    if not a.no_update_check:
        check_for_update()

    s = Shaper(a)
    def bye(*_):
        print("\n\n  stopping - removing all shaping...")
        s.remove()
        print("  done. your connection is unrestricted again.\n")
        sys.exit(0)
    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    # Closing the terminal window sends SIGHUP. Without this the process dies
    # and the speed limit stays applied - the user's connection would be
    # throttled with no visible cause and no obvious way to undo it.
    signal.signal(signal.SIGHUP, bye)
    try:
        s.run()
    except Exception:
        s.remove(); raise


if __name__ == "__main__":
    main()
