![A girl in a cluttered brass workshop ignores a wall of speed gauges — every needle pinned in the green, one draped in cobwebs — and instead times a returning clockwork sparrow with a stopwatch, ear pressed to a listening pipe. Beside her, one plain instrument with its needle well off the rest.](docs/hero.jpg)

# autoshape

**Keeps a tethered hotspot usable while something is uploading.**

Working off a phone hotspot or a MiFi? Then you know the failure: a backup, a
screen recording or a big upload starts, and suddenly *nothing* loads. Pages
hang. Calls stutter. A speed test still insists your connection is fine.

`autoshape` fixes that — **and it adapts in real time, which is the whole
point.** A fixed speed limit is wrong almost all the time on a cellular link,
and when it's wrong it does real harm. `autoshape` measures how long a small
web request actually takes, and moves your upload limit up and down to match
whatever the tower is giving you right now.

Measured on a 4G MiFi with a large upload running throughout:

| | without | with |
|---|---|---|
| time to load a page | 1739 ms | **554 ms** |
| requests taking over 1 second | 100% | **11%** |

---

## Install

macOS only. No dependencies — it uses the Python 3 that macOS already ships.

**Homebrew** — recommended, because `brew upgrade` then keeps it current:

```sh
brew install nicolasdao/tap/autoshape
```

**Or the installer**, if you don't use Homebrew:

```sh
curl -fsSL https://raw.githubusercontent.com/nicolasdao/autoshape/master/install.sh | sh
```

Or from a clone:

```sh
git clone https://github.com/nicolasdao/autoshape.git
cd autoshape && ./install.sh
```

Pick one. Installing both leaves two copies on your `PATH`, and which one runs
depends on the order of `/opt/homebrew/bin` and `/usr/local/bin` — confusing in
exactly the way a tool you run as root should not be.

The installer defaults to `/usr/local`. To put it somewhere else:

```sh
curl -fsSL https://raw.githubusercontent.com/nicolasdao/autoshape/master/install.sh \
  | PREFIX=$HOME/.local sh
```

`--update` reinstalls into whichever prefix it finds itself in, so you only
have to say this once. (Homebrew ignores `PREFIX` — it manages its own.)

## Commands

```sh
sudo autoshape              # start it — then press Ctrl-C to stop
sudo autoshape --off        # clear anything left behind (if it was force-killed)
autoshape --version         # what you're running (no sudo needed)
sudo autoshape --update     # update — installer installs only, see Updating
```

That's the entire interface. It needs `sudo` because changing packet scheduling
is a privileged operation. `Ctrl-C` removes everything it changed and tells you
so — and nothing survives a reboot either way.

**`sudo` works the same on a Homebrew install** — macOS leaves your `PATH`
alone under `sudo`, so `sudo autoshape` finds `/opt/homebrew/bin/autoshape`
normally. (Most Linux distributions *do* restrict it, which is why this is
worth stating rather than assuming.) If you have hardened `sudoers` with a
`secure_path` yourself, use `sudo "$(which autoshape)"`.

---

## Why adaptive matters

This is the part that makes `autoshape` worth running rather than typing a
`dnctl` command by hand.

**Cellular capacity is not a number, it's a moving target.** On the connection
this was built against, measured across a single afternoon:

```
   6.2 Mbps  ····  8.9  ····  15.6  ····  28.7  ····  90.2 Mbps
```

A 15× swing. **Any fixed limit you choose is wrong most of the time** — set it
at 5 and you're strangled when the tower is good; set it at 40 and you're
unprotected when it's bad.

**And a fixed limit doesn't just under-perform, it actively harms you.** With
the same link running at 90 Mbps and no congestion at all, every fixed cap we
measured made things *worse* than doing nothing:

| | page load |
|---|---|
| no limit at all | **150 ms** |
| capped at 12 Mbps | 307–535 ms |

Capping a healthy link manufactures the exact bottleneck you were trying to
avoid. So a static limit is something you'd have to keep deciding whether to
turn on — which realistically means you don't run it at all.

**`autoshape` is safe to just leave running.** When your connection is healthy
it stays out of the way; when the queue starts filling it eases off; when the
tower recovers it opens back up. You never pick a number.

---

## The problem

Your uplink is the narrow part, and *everything* shares it — including the small
replies your downloads depend on.

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

The hotspot's buffer is sized for throughput, not responsiveness. When it fills,
every small request queues behind megabytes of somebody's upload.

**This is also why your downloads die.** Downloading isn't passive — for every
chunk you receive, your machine must send back an acknowledgement. Those are
stuck in the same queue. The download stalls not because data can't come *down*,
but because your receipts can't get *up*.

### The fix

Cap your own upload slightly below what the link can actually carry. The
bottleneck moves off the hotspot — whose queue is huge and not yours to control
— and onto your Mac, where the queue is deliberately tiny.

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

**Probably not, if you're on home fibre or cable** — and definitely not if you
control your own router, where [SQM / fq_codel](https://www.bufferbloat.net/)
is the better answer. Run `autoshape` once and watch: if it sits on **GOOD** the
whole time, you don't need it.

**Rule of thumb:** if starting an upload makes everything else feel broken, this
will help. If it doesn't, it won't.

---

## What you'll see

```
  autoshape  adaptive uplink guard on en0 · limits 2-50 Mbps
  press Ctrl-C to stop and remove all shaping

  GOOD       internet is responsive

  speed limit     24.0 Mbps  ██████░░░░░░░░  steady
  in use           3.2 Mbps  ██░░░░░░░░░░░░   13% · headroom

  delay              34 ms   █░░░░░░░░░░░░░  +6 over 28 resting
  worst seen         34 ms
```

- **GOOD** (green) — responsive, nothing to do
- **BUSY** (yellow) — delay climbing, watching closely
- **CONGESTED** (red) — queue filling, actively easing off

**`delay` is the line that matters** — how long a small request takes right now,
which is what makes a page feel instant or broken. `in use` tells you whether
the limit is actually biting (`limiting`) or sitting above your traffic
(`headroom`).

---

## Prior art, and what you could do by hand

**The approach is not original, and the credit belongs to
[cake-autorate](https://github.com/lynxthecat/cake-autorate)** by lynxthecat —
the reference implementation of latency-driven adaptive shaping. Several details
here come straight from it: the asymmetric delay baseline, N-of-M congestion
voting, serialization compensation, and the load gate. **If you run OpenWrt, use
cake-autorate instead** — it runs on the router, it has CAKE's per-flow
queueing, and it's more mature than this.

**`autoshape` exists because you can't install OpenWrt on a hotel MiFi.** It
puts the same idea on the client, where nothing else did.

**And yes, you can do a static version by hand** with tools macOS already ships:

```sh
sudo dnctl pipe 1 config bw 5Mbit/s queue 16
echo "dummynet out quick on en0 from any to any pipe 1" | sudo pfctl -a myrule -f -
sudo pfctl -E
```

That genuinely works — measured at 1445 ms → 529 ms. What it can't do is pick
the right number, change it as the tower changes, or get out of the way when
your connection is fine. See [Why adaptive matters](#why-adaptive-matters).

Other macOS tools solve adjacent problems, not this one:
[TripMode](https://tripmode.ch/) blocks apps on metered connections,
[Bytetally](https://bytetally.app/en/limit-app-bandwidth-mac/) sets static
per-app limits, and Apple's Network Link Conditioner exists to *simulate* bad
networks for testing.

---

## How it works

**It never measures your bandwidth** — it measures *delay* and infers congestion:

```
   delay rising while you're uploading  →  the queue is filling  →  slow down
   delay flat and you're using the pipe →  there's room          →  speed up
   nothing being sent                   →  no information        →  drift to base
```

Three details that are easy to get wrong:

**It doesn't use ping.** ICMP is a single small packet that slips past the
queue. On the reference link, ping showed **+14 ms and 0% loss** while real web
requests took **2672 ms**. Packet capture showed why: 100% of the delay sits
between the server replying and your machine responding, so a conversation that
must reply repeatedly gets punished while a lone ping sails through.
`autoshape` times a small request on a persistent TCP connection — what your
browser actually experiences.

**It compensates for its own queue.** At a low limit, one full queue is itself
tens of milliseconds. Without accounting for that, the shaper reads its own
buffer as congestion, slows down, makes its own queue slower, and collapses.

**It won't cut while delay is falling.** Lowering the limit doesn't drain the
backlog instantly — that takes seconds. Cutting again during the drain means
reacting to a problem you already solved, and it's the fastest way to throttle
yourself to zero.

---

## Updating

**However you installed it, `autoshape --version` tells you what you have** and
the daily check tells you the right command for *your* install — you don't have
to remember which one you used.

| installed with | update with |
|---|---|
| Homebrew | `brew update && brew upgrade autoshape` |
| the installer | `sudo autoshape --update` |

`autoshape` knows which it is. On a Homebrew copy, `--update` refuses and points
you at `brew upgrade` rather than running the installer over the top of the brew
prefix — which would either be silently ignored (Apple Silicon) or overwrite
brew-managed files (Intel, where the brew prefix *is* `/usr/local`).

It checks GitHub for a newer version **at most once a day** and prints a single
line if one exists. Never blocks start-up, never sends anything about you. Turn
it off with `--no-update-check` or `export AUTOSHAPE_NO_UPDATE_CHECK=1`.

---

## Platform support

| | status |
|---|---|
| **macOS** | supported and tested (`pfctl` + `dnctl`) |
| **Linux** | not implemented — would work *better* here, since `tc` + CAKE has real per-flow queueing |
| **Windows** | not planned — no native equivalent |

The control logic in `control.py` is portable, dependency-free Python; only the
actuation layer is platform-specific. A Linux backend is a small adapter.
Contributions welcome.

---

## What it changes on your system

Worth knowing before running anything as root:

- Creates a `dummynet` pipe (number 2) via `dnctl`
- Loads a rule into its own `pf` anchor, `com.apple/shape`, sending outbound
  traffic on your active Wi-Fi interface through that pipe
- **Exempts your local network**, so printers, AirPlay and local servers are
  untouched
- Enables `pf` if it wasn't already

No config files, no daemon, nothing at boot. `Ctrl-C` or `--off` reverses all of
it.

**Uninstall:**

```sh
sudo autoshape --off                  # first, undo any shaping

brew uninstall autoshape              # if installed with Homebrew
sudo rm -rf /usr/local/lib/autoshape /usr/local/bin/autoshape   # if installed with the installer
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

Scenarios cover starting above and below capacity, healthy links that must be
left alone, capacity collapsing and recovering mid-run, idle links, competing
downloads, and bursty traffic.

---

## Releasing

For maintainers. One command, and both install channels follow:

```sh
./scripts/release.sh 1.1.0
```

It bumps `VERSION`, tags and pushes. `.github/workflows/release.yml` then
publishes the GitHub release, hashes the tarball and pushes the regenerated
formula to [nicolasdao/homebrew-tap](https://github.com/nicolasdao/homebrew-tap).
`packaging/autoshape.rb` is the source of truth — the tap copy is generated, so
never hand-edit it. There is no `sha256` to paste by hand anywhere.

Auth is already configured: an SSH deploy key scoped to `homebrew-tap` alone,
with its private half stored as the `TAP_DEPLOY_KEY` secret on this repo. It
never expires. To rotate it, generate a new `ed25519` pair, replace the deploy
key on `homebrew-tap`, and re-run `gh secret set TAP_DEPLOY_KEY`.

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
