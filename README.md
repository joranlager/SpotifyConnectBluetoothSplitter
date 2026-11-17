# SpotifyConnectBluetoothSplitter

A Python app that makes your PC a **Spotify Connect** device and plays the
same music on **several Bluetooth speakers at once**.

It has two parts:

- **`spconnect/`** — a Spotify Connect receiver written in Python. It appears in
  Spotify's device list, takes play/pause/skip/seek/queue/volume commands, and
  downloads, decrypts and decodes the audio. No external program is needed.
- **`splitter/`** — the app: it copies the decoded audio to one buffer per
  speaker, keeps the speakers aligned, and serves a web UI to control it.

```
Spotify app ──Connect──> spconnect (receiver + decoder) ──PCM──> FanOut ──┬──> buffer -> speaker 1
                                                                          ├──> buffer -> speaker 2
                                                                          └──> buffer -> ...
```

## Requirements

- Windows 10/11 (the code is cross-platform, but speaker matching is tuned for WASAPI)
- Python 3.10+
- A Spotify **Premium** account
- Bluetooth speakers already **paired and connected** in Windows Settings

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json
```

## Run

```bash
python -m splitter run --config config.json
```

Then:

1. Open **http://localhost:8080** and add your speakers from *Available devices*.
2. Press **Start**. The first time, Windows Firewall asks whether Python may
   accept connections: allow it on private networks, or Spotify cannot find the
   device.
3. In Spotify (phone or PC, on the same network), open the device picker and
   choose **Bluetooth Splitter**.

After the first time, the app stays signed in: it saves a login token in
`.spconnect-state.json` next to the config file. Treat that file like a
password and do not share or commit it (it is in `.gitignore`).

Options: `--host 0.0.0.0` and `--port 8080` override the web settings,
`--no-web` runs without the web UI, and `--name` overrides `device_name`.
`python -m splitter list` prints the audio outputs Windows reports.

## Web UI

- **Control**: start, stop or restart the device; play, pause, next and previous.
- **Monitor**: what is playing and its position, throughput history, and per
  speaker latency, buffer level and underruns.
- **Speakers**: per-speaker volume and mute, live. Delay changes restart the
  stream. Add speakers from the detected devices list, or remove them.
- **Settings**: device name, quality, buffer, speaker alignment, autostart and
  advanced options. Changes are saved to `config.json`; the stream restarts if
  it is running.

The UI is one file served by the app, with no external dependencies. The web
server listens on `127.0.0.1` by default, so only this PC can reach it. Setting
`web_host` to `0.0.0.0` lets other devices on your network control it. There is
no login, so do that only on a trusted network.

### API

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/status` | Live state, now playing and per-speaker metrics |
| GET | `/api/outputs` | Audio devices visible to Windows |
| GET / PUT | `/api/config` | Read or replace the configuration (saved to disk) |
| POST | `/api/control` | `{"action": "start" \| "stop" \| "restart" \| "play" \| "pause" \| "next" \| "prev"}` |
| PATCH | `/api/speaker` | `{"match": "...", "volume"?, "muted"?, "delay_ms"?}` |

Requests that change state must send `Content-Type: application/json`.

## Configuration

| Key | Meaning |
| --- | --- |
| `device_name` | Name shown in Spotify's device list |
| `receiver` | `builtin` (default, the Python receiver) or `librespot` (external program) |
| `bitrate` | 96, 160 or 320 kbit/s |
| `buffer_seconds` | Largest per-speaker buffer before old audio is dropped |
| `auto_latency_compensation` | Delays faster speakers so all speakers line up |
| `autostart` | Start the device when the app launches |
| `web_host` / `web_port` | Web UI address (default `127.0.0.1:8080`) |
| `speakers[].match` | Case-insensitive part of the output device name (unique) |
| `speakers[].delay_ms` | Manual extra delay for one speaker (fine-tune by ear) |
| `speakers[].volume` | 0.0–1.0 per-speaker volume, set live from the UI |
| `speakers[].muted` | Mute state, set live from the UI |
| `librespot_path`, `librespot_args`, `cache_dir` | Only for `receiver: "librespot"` |

## How the sync works

- All speakers receive identical PCM (16-bit, 44.1 kHz, stereo).
- Each speaker's output runs on its own realtime stream fed from its own buffer.
- The receiver decodes only as fast as the speakers consume, keeping about a
  quarter of a second queued, so the stream cannot drift away from the outputs.
- Bluetooth latency differs per speaker. With `auto_latency_compensation`, the
  app reads each stream's reported latency and gives the faster speakers a head
  of silence. That lead is restored after every pause, seek and track change.
  Reported latency for Bluetooth is often approximate, so if you hear an echo,
  adjust `delay_ms` for the speaker that is early.
- If a speaker falls behind, its oldest audio is dropped, and if it runs dry it
  plays silence. Other speakers are never blocked.
- A speaker that is off or unavailable at startup is skipped with a warning.

## The spconnect library

`spconnect` can be used on its own:

```python
from spconnect import Receiver, ReceiverConfig, NullSink

receiver = Receiver(ReceiverConfig(device_name="Living Room", state_path="state.json"), NullSink())
receiver.start()            # now visible in Spotify's device picker
...
receiver.stop()
```

Audio goes to a *sink*: any object with `write(pcm_bytes)`, `pending_ms()` and
`flush()`. `NullSink` discards it in real time.

What it implements: zeroconf discovery and login, the access point connection
(Diffie-Hellman handshake, Shannon cipher), client and access tokens, the dealer
websocket, Connect state (transfer, play, pause, seek, next/previous, queue,
shuffle, repeat, volume, autoplay), track metadata, audio keys, CDN download,
AES decryption, Ogg Vorbis decoding, loudness normalisation and gapless track
changes.

It is pure Python. Its dependencies are ordinary pip packages; three of them
ship compiled code (`cryptography` for AES, `soundfile` for Vorbis decoding,
`numpy`). There is no pure-Python Vorbis decoder fast enough to play in real time.

See [spconnect/NOTICE.md](spconnect/NOTICE.md) for the origin of the protocol
definitions and the licence of the code this is derived from.

## Known limitations

- **Sync is approximate**, usually within tens of milliseconds, but Bluetooth
  does not expose exact timing. Expect a small echo on some speaker pairs.
- **Speaker drop-outs** on Bluetooth are not auto-reconnected; press Restart
  after reconnecting a speaker.
- Podcasts in formats other than Ogg Vorbis or MP3, local files and videos are
  skipped. Jam sessions and the DJ are not supported.
- The receiver uses reverse-engineered protocols. Spotify can change them at any
  time, and using it may be against Spotify's terms of service.

## Development

```bash
pip install pytest
pytest
```

The tests need no Spotify account, network or speakers. They cover the cipher,
login blob handling, audio download/decrypt/decode, the Connect state machine,
the player, discovery, command handling, the fan-out and the web API.

- `scripts/gen_proto.py` regenerates `spconnect/proto/*_pb2.py` from a librespot
  checkout.
- The web UI is a single file, `splitter/web/index.html` (plain HTML, CSS and
  JS, no build step). The server reads it on each page load.
