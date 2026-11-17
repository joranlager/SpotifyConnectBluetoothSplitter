# spotify-headless

Runs the **official Spotify desktop client** headless on **Linux x86_64** and
hands its decoded audio to another program. Spotify does all the decoding and
DRM itself; this tool only does the wiring around it, the way a mixer app would.

The desktop client shows up in Spotify's device list while it is running and
signed in, so a phone can transfer playback to it like to any other device.

```
phone ──Connect──> official Spotify client ──> virtual sink ──monitor──> parec ──PCM──> your splitter
                   (on an Xvfb display)       (PULSE_SINK)               (stdout or FIFO)
```

What it does, and keeps running (restarting with a backoff when something dies):

1. Creates a **null sink** (`spotify_headless`) through `pactl`, so it works with
   PulseAudio and with PipeWire's pulse layer. It removes the sink on exit.
2. Starts **Xvfb** on a free display, with a private auth cookie (no `-ac`).
3. Starts **Spotify** on that display with `PULSE_SINK` set to the sink, and
   moves any stream that still ends up elsewhere onto the sink.
4. Optionally runs **parec** on the sink's monitor and writes raw PCM to stdout
   or a FIFO: signed 16-bit little-endian, stereo, 44.1 kHz by default.
5. Optionally runs **x11vnc** on the loopback, to sign in once.

## Requirements

- Linux x86_64 with a user audio session (PipeWire with `pipewire-pulse`, or PulseAudio)
- The official Spotify client: Spotify's apt repository, snap, or flatpak
- `xvfb`, `pulseaudio-utils` (for `pactl` and `parec`), and `x11vnc` for the first login
- A Spotify account that can use Spotify Connect (Premium)
- Rust to build it: `cargo build --release`

```bash
sudo apt install xvfb pulseaudio-utils x11vnc
```

## Use

Check the machine first:

```bash
spotify-headless doctor --pcm-out - --vnc-port 5900
```

### 1. Sign in once

The client has to be signed in before it appears as a Connect device. Start it
with VNC, tunnel the port from your own computer, and sign in:

```bash
spotify-headless run --vnc-port 5900 --data-dir ~/.local/share/spotify-headless
```

```bash
ssh -L 5900:127.0.0.1:5900 user@your-board
```

Then connect a VNC viewer to `localhost:5900` and log in. If your account has no
password (you normally use Google, Facebook or Apple), the client's browser
login cannot open on a headless board; set a password for the account on
Spotify's website first. The login is stored in `--data-dir`, so later runs
need no VNC. Stop with Ctrl-C.

### 2. Run it

```bash
spotify-headless run --data-dir ~/.local/share/spotify-headless --pcm-out /run/user/1000/spotify.pcm
```

`--pcm-out -` writes the PCM to stdout (all log lines go to stderr), so you can pipe it:

```bash
spotify-headless run --data-dir ~/.local/share/spotify-headless --pcm-out - | your-splitter --format s16le --rate 44100 --channels 2
```

With a FIFO, the capture waits for a reader and starts again when the reader
reconnects. Without `--pcm-out` nothing is captured; read the sink's
`spotify_headless.monitor` source yourself.

Open Spotify on your phone and pick this machine in the device list. For a
service that starts at boot, see [spotify-headless.service](spotify-headless.service).

## Options

| Option | Meaning |
| --- | --- |
| `--sink-name NAME` | Virtual sink name (letters, digits, `_`, `-`); default `spotify_headless` |
| `--rate HZ` | Sink and capture sample rate; default 44100 |
| `--pcm-out TARGET` | `-` for stdout, or a FIFO path |
| `--vnc-port PORT` | Run x11vnc on `127.0.0.1:PORT` |
| `--resolution WxHxD` | Virtual display size; default `1280x720x24` |
| `--display-start N` | First X display number to try; default 99 |
| `--spotify-bin PATH` | Spotify executable; default `spotify` |
| `--spotify-arg ARG` | Extra argument for Spotify (repeatable) |
| `--data-dir DIR` | Keep Spotify's config and cache here (sets `XDG_CONFIG_HOME`/`XDG_CACHE_HOME`) |
| `--log-dir DIR` | Logs: `xvfb.log`, `spotify.log`, `parec.log`, `x11vnc.log` |

## Notes and limits

- **Not tested against a real Spotify client yet.** The pure logic has unit
  tests and the Unix code type-checks for `x86_64-unknown-linux-gnu`, but the
  whole chain has not been run on a Linux machine with Spotify installed.
- **The official client is not built for headless use.** It may need flags on a
  board without a GPU (pass them with `--spotify-arg`; `LIBGL_ALWAYS_SOFTWARE=1`
  is set already), and an update could change how it behaves. When running as
  root, the client may need `--no-sandbox`.
- **Do not run it next to your own Spotify session** under the same user and
  config: two instances fight over the same config folder. `--data-dir` keeps
  this one separate.
- **Volume and metadata** are not part of the PCM stream. The app's volume
  changes the level of what you receive, so keep it fixed.
- This only routes and captures the system audio of a client you run yourself;
  it does not touch Spotify's protocol or decryption. Check that your use fits
  Spotify's terms.

## Development

```bash
cargo test                                         # pure-logic tests, run on any OS
cargo clippy --target x86_64-unknown-linux-gnu     # type-check the Linux code
```
