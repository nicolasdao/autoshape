# autoshape

**Keeps a tethered hotspot usable while something is uploading.**

If you work from a phone hotspot or a MiFi, you know the failure: someone starts
a backup, a screen recording, or a big file upload, and suddenly *nothing* loads.
Pages hang. Video calls stutter. A speed test still says your connection is fine.

`autoshape` fixes that. It watches how long a small web request actually takes
and adjusts how fast your Mac is allowed to upload, in real time, so the queue
never gets deep enough to hurt you.

Measured on a 4G MiFi, with a large upload running throughout:

| | without | with |
|---|---|---|
| time to load a page | 1739 ms | **554 ms** |
| requests taking over 1 second | 100% | **11%** |

---

## The problem

Your uplink is the narrow part, and it is shared by *everything* — including the
small replies your downloads depend on.

```
   YOUR MAC                    HOTSPOT                     INTERNET

  ┌───────────────┐         ┌──────────────────────┐
  │ big upload    │ ══════▶ │ ████████████████████ │ ──────────▶
  │ backup, video │  fast   │                      │   slow
  │ screen record │  wifi   │  a queue that holds  │   cellular
  └───────────────┘         │  SECONDS of data     │
                            │ ████████████████████ │
  ┌───────────────┐         └──────────────────────┘
  │ you click a   │ ───┐               ▲
  │ link          │    └───────────────┘
  └───────────────┘      your click joins the BACK of that queue
                         and waits behind all of it
```

The hotspot's buffer is sized for throughput, not for responsiveness. When it
fills, every small request queues behind megabytes of somebody's upload.

**And this is why your downloads die too.** Downloading is not passive — for
every chunk you receive, your machine must send back an acknowledgement. Those
acknowledgements are stuck in the same queue. The download stalls not because
data can't come *down*, but because your receipts can't get *up*.

### The fix

Cap your own upload slightly below what the link can actually carry. The
bottleneck moves from the hotspot — whose queue is huge and not yours to control
— onto your Mac, where the queue is deliberately tiny.

```
  ┌───────────────┐         ┌──────────────────────┐
  │ big upload    │ ──┐     │ ██                   │ ──────────▶
  └───────────────┘   │     │                      │
                      ▼     │  queue stays nearly  │
              ┌──────────┐  │  empty               │
              │ autoshape│  │                      │
              │ 10ms     │  └──────────────────────┘
              │ queue    │            ▲
              └──────────┘            │
  ┌───────────────┐   ▲               │
  │ you click a   │ ──┘    ───────────┘
  │ link          │        goes almost straight through
  └───────────────┘
```

You give up a little upload speed. You get your connection back.

---

## Is this for you?

**Yes, if you're tethering.** Phone hotspot, MiFi, 4G/5G router, café tethering,
campervan, hotel dongle. Cellular uplinks are narrow and their buffers are
enormous, which is exactly the condition this fixes.

**Probably not, if you're on home fibre or cable.** A good fixed line with a
modern router usually has sensible queue management already. `autoshape` would
just cost you upload speed for no benefit. Run it once and watch — if it sits on
**GOOD** the whole time, you don't need it.

**Rule of thumb:** if starting an upload makes everything else feel broken,
this will help. If it doesn't, it won't.

---

## Install

macOS only — see [Platform support](#platform-support).

```sh
curl -fsSL https://raw.githubusercontent.com/nicolasdao/autoshape/main/install.sh | sh
```

Or from a clone:

```sh
git clone https://github.com/nicolasdao/autoshape.git
cd autoshape && ./install.sh
```

No dependencies. It's Python 3 from the standard library, which macOS already
has.

---

## Use

**Start it:**

```sh
sudo autoshape
```

**Stop it:** press `Ctrl-C`. It removes everything it changed and tells you so.

That's the whole interface. It needs `sudo` because changing packet scheduling
is a privileged operation.

### What you'll see

```
  autoshape  adaptive uplink guard on en0 · limits 2-50 Mbps
  press Ctrl-C to stop and remove all shaping

  GOOD       internet is responsive

  speed limit     24.0 Mbps  ██████░░░░░░░░  steady
  in use           3.2 Mbps  ██░░░░░░░░░░░░   13% · headroom

  delay              34 ms   █░░░░░░░░░░░░░  +6 over 28 resting
  worst seen         34 ms
```

- **GOOD** (green) — your connection is responsive, nothing to do
- **BUSY** (yellow) — delay is climbing, watching closely
- **CONGESTED** (red) — the queue is filling, actively easing off

**`delay` is the line that matters.** That's how long a small request takes right
now, and it's what makes a page feel instant or broken. `in use` tells you
whether the limit is actually biting (`limiting`) or just sitting above your
traffic (`headroom`).

### If something goes wrong

If the process is force-killed, the speed limit could survive it. To clear
anything left behind:

```sh
sudo autoshape --off
```

Nothing survives a reboot either way.

---

## How it works

**It never measures your bandwidth.** Bandwidth on a cellular link is a moving
target — the connection this was built on ranged from 6 Mbps to 90 Mbps in a
single afternoon. Any fixed limit is wrong most of the time.

Instead it measures *delay*, and infers congestion from it:

```
   delay rising while you're uploading  →  the queue is filling  →  slow down
   delay flat and you're using the pipe →  there's room          →  speed up
   nothing being sent                   →  no information        →  drift to base
```

Three details that took real effort to get right, and that most naive versions
get wrong:

**It doesn't use ping.** ICMP is a single small packet that slips past the
queue. Measured on the reference link: ping showed **+14 ms** while real web
requests took **2672 ms**. Packet capture showed why — 100% of the delay sits
between the server replying and your machine responding, so a conversation that
must reply repeatedly gets punished while a lone ping does not. `autoshape`
times a small request on a persistent TCP connection, which feels exactly what
your browser feels.

**It compensates for its own queue.** At a low speed limit, one full queue is
itself tens of milliseconds. A version that doesn't account for this reads its
own buffer as congestion, slows down, makes its own queue slower, and collapses
to nothing.

**It won't cut while delay is falling.** Lowering the limit doesn't drain the
backlog instantly — that takes seconds. Cutting again during the drain means
reacting to a problem you already solved, and it's the single fastest way to
throttle yourself to zero.

---

## Platform support

| | status |
|---|---|
| **macOS** | supported and tested (uses `pfctl` + `dnctl`) |
| **Linux** | not implemented — would work *better* here, since `tc` + CAKE has real per-flow queueing |
| **Windows** | not planned — no native equivalent to `dnctl` |

The control logic in `control.py` is portable, dependency-free Python. Only the
actuation layer is platform-specific, so a Linux backend is a small adapter.
Contributions welcome.

---

## What it changes on your system

Worth knowing before you run something as root:

- Creates a `dummynet` pipe (number 2) via `dnctl`
- Loads a rule into its own `pf` anchor, `com.apple/shape`, that sends outbound
  traffic on your active Wi-Fi interface through that pipe
- **Exempts your local network**, so printers, AirPlay and local servers are
  untouched
- Enables `pf` if it wasn't already

It touches nothing else, writes no config files, installs no daemon, and starts
nothing at boot. `Ctrl-C` or `--off` reverses all of it.

**Uninstall:**

```sh
sudo autoshape --off
sudo rm -rf /usr/local/lib/autoshape /usr/local/bin/autoshape
```

---

## Tests

The control loop is validated against a simulated bottleneck rather than by
trial and error on a live connection. Both suites run in seconds, need no root,
and touch no network settings.

```sh
python3 tests/test_control.py          # 9 scenarios
python3 tests/test_control.py --sweep  # 50 link types: 1-500 Mbps x 5-600 ms
python3 tests/test_sensor.py           # the delay sensor itself (~40s)
```

The scenarios cover starting above and below capacity, links that are already
healthy and must be left alone, capacity collapsing and recovering mid-run, idle
links, competing downloads, and bursty traffic.

---

## Limitations

- **It can only shape what you send.** If someone else on your hotspot is
  saturating the link, or a big download is the cause, there's nothing to
  throttle — it detects this and holds rather than cutting uselessly.
- **Starting far above capacity on a very slow link takes a while to recover.**
  The backlog drains at `capacity − limit`, which is physics, not tuning.
- **`dummynet` is a single FIFO.** Linux's CAKE would do better by letting small
  requests overtake bulk traffic. This gets most of the benefit, not all of it.

---

## License

BSD 3-Clause. Copyright (c) 2026, Nicolas Dao. See [LICENSE](LICENSE).
