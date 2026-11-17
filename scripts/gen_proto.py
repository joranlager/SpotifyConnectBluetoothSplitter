"""Regenerate spconnect/proto/*_pb2.py from librespot's protocol definitions.

Usage:
    python scripts/gen_proto.py <path-to-librespot-checkout>/protocol/proto

The .proto files come from librespot (https://github.com/librespot-org/librespot,
MIT licence). Only the files this library needs, plus what they import, are
copied. They are flattened into one directory so the generated modules can use
relative imports and live inside the `spconnect.proto` package.

Requires: pip install grpcio-tools
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOTS = [
    "keyexchange.proto",
    "authentication.proto",
    "connect.proto",
    "player.proto",
    "devices.proto",
    "media.proto",
    "transfer_state.proto",
    "context.proto",
    "context_page.proto",
    "context_track.proto",
    "context_player_options.proto",
    "playback.proto",
    "session.proto",
    "queue.proto",
    "restrictions.proto",
    "play_origin.proto",
    "suppressions.proto",
    "spotify/clienttoken/v0/clienttoken_http.proto",
    "spotify/login5/v3/login5.proto",
    "storage-resolve.proto",
    "metadata.proto",
    "extended_metadata.proto",
    "extension_kind.proto",
    "entity_extension_data.proto",
    "audio_files_extension.proto",
    "autoplay_context_request.proto",
    "user_attributes.proto",
    "explicit_content_pubsub.proto",
]

IMPORT_RE = re.compile(r'^(\s*import\s+(?:public\s+)?)"([^"]+)"\s*;', re.MULTILINE)
REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "spconnect" / "proto"
SRC_OUT = OUT / "src"


def collect(proto_dir: Path) -> dict[str, Path]:
    """Map flattened file name -> source path, following imports."""
    found: dict[str, Path] = {}
    todo = list(ROOTS)
    while todo:
        rel = todo.pop()
        if rel.startswith("google/protobuf/"):
            continue  # well-known types ship with the protobuf runtime
        src = proto_dir / rel
        if not src.exists():
            raise SystemExit(f"missing proto: {src}")
        flat = Path(rel).name
        if flat in found:
            if found[flat] != src:
                raise SystemExit(f"name collision when flattening: {flat}")
            continue
        found[flat] = src
        for _, imported in IMPORT_RE.findall(src.read_text(encoding="utf-8")):
            todo.append(imported)
    return found


def flatten_imports(text: str) -> str:
    def repl(m: re.Match) -> str:
        path = m.group(2)
        if path.startswith("google/protobuf/"):
            return m.group(0)
        return f'{m.group(1)}"{Path(path).name}";'

    return IMPORT_RE.sub(repl, text)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    proto_dir = Path(sys.argv[1]).resolve()
    files = collect(proto_dir)

    if SRC_OUT.exists():
        shutil.rmtree(SRC_OUT)
    SRC_OUT.mkdir(parents=True)
    for old in OUT.glob("*_pb2.py"):
        old.unlink()

    for flat, src in sorted(files.items()):
        (SRC_OUT / flat).write_text(
            flatten_imports(src.read_text(encoding="utf-8")), encoding="utf-8", newline="\n"
        )

    from grpc_tools import protoc
    import grpc_tools

    well_known = Path(grpc_tools.__file__).parent / "_proto"
    args = [
        "protoc",
        f"-I{SRC_OUT}",
        f"-I{well_known}",
        f"--python_out={OUT}",
        *[str(SRC_OUT / f) for f in sorted(files)],
    ]
    rc = protoc.main(args)
    if rc != 0:
        return rc

    # Make the generated modules importable from inside the package.
    rel = re.compile(r"^import (\w+_pb2) as ", re.MULTILINE)
    for gen in OUT.glob("*_pb2.py"):
        text = gen.read_text(encoding="utf-8")
        gen.write_text(rel.sub(r"from . import \1 as ", text), encoding="utf-8", newline="\n")

    print(f"generated {len(list(OUT.glob('*_pb2.py')))} modules in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
