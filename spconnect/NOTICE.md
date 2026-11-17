# Third-party notices for spconnect

spconnect is a Python implementation of a Spotify Connect receiver. Its protocol
knowledge comes from the open-source **librespot** project, and parts of this
package are derived from it:

- `proto/src/*.proto` are copied from librespot's `protocol/proto` directory
  (import paths flattened by `scripts/gen_proto.py`). `proto/*_pb2.py` are
  generated from them.
- `crypto.py` (`Shannon`) is a Python port of the `shannon` crate.
- `state.py` and `receiver.py` follow the design of librespot's `connect` crate
  (`state/*`, `spirc.rs`), and `ap.py`, `http.py`, `dealer.py`, `discovery.py`
  and `audio.py` implement the same protocols as its `core`, `discovery`,
  `audio` and `playback` crates.

Both projects are published under the MIT licence, reproduced below.

- librespot: https://github.com/librespot-org/librespot — Copyright (c) 2015 Paul Lietar
- shannon: https://github.com/plietar/rust-shannon — Copyright (c) 2016 Paul Lietar

```
The MIT License (MIT)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

Spotify is a trademark of Spotify AB. This project is not affiliated with or
endorsed by Spotify. It talks to Spotify's services through reverse-engineered
protocols and needs a Spotify Premium account; using it may be against
Spotify's terms of service.
