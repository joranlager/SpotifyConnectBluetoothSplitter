import io
import os

import numpy as np
import pytest
import soundfile as sf

from spconnect import audio
from spconnect.audio import (
    SAMPLE_RATE,
    SPOTIFY_OGG_HEADER_END,
    AudioLoader,
    CdnFile,
    DecryptedStream,
    Unavailable,
    is_restricted,
    normalisation_factor,
    select_file,
)
from spconnect.http import new_http_session
from spconnect.ids import SpotifyId
from spconnect.proto import audio_files_extension_pb2 as audio_files_pb
from spconnect.proto import extension_kind_pb2 as kind_pb
from spconnect.proto import metadata_pb2
from spconnect_helpers import MemorySource, RangeServer, encode_vorbis, sine, spotify_file

F = metadata_pb2.AudioFile
TRACK_URI = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"


@pytest.fixture(scope="module")
def ogg():
    return encode_vorbis(sine(6.0))


def decode_all(fileobj) -> np.ndarray:
    with sf.SoundFile(fileobj) as f:
        return f.read(dtype="float32", always_2d=True)


def test_decrypted_stream_decodes_identically_to_plain_file(ogg):
    key = os.urandom(16)
    source = MemorySource(spotify_file(ogg, key))
    stream = DecryptedStream(source, key, SPOTIFY_OGG_HEADER_END)
    assert stream.read(4) == b"OggS"
    stream.seek(0)
    decoded = decode_all(stream)
    expected = decode_all(io.BytesIO(ogg))
    assert decoded.shape == expected.shape
    assert np.array_equal(decoded, expected)


def test_decrypted_stream_seek_and_tell(ogg):
    key = os.urandom(16)
    source = MemorySource(spotify_file(ogg, key))
    stream = DecryptedStream(source, key, SPOTIFY_OGG_HEADER_END)
    assert stream.seek(0, io.SEEK_END) == len(ogg)
    assert stream.read(10) == b""
    stream.seek(100)
    assert stream.read(50) == ogg[100:150]
    assert stream.tell() == 150
    stream.seek(-20, io.SEEK_CUR)
    assert stream.read(5) == ogg[130:135]


def test_wrong_key_does_not_decode(ogg):
    source = MemorySource(spotify_file(ogg, os.urandom(16)))
    stream = DecryptedStream(source, os.urandom(16), SPOTIFY_OGG_HEADER_END)
    with pytest.raises(Exception):
        sf.SoundFile(stream)


def test_download_error_is_recorded_not_raised(ogg):
    source = MemorySource(ogg)
    stream = DecryptedStream(source, None)
    source.fail = True
    assert stream.read(100) == b""
    assert isinstance(stream.error, OSError)


def test_cdn_file_fetches_ranges_and_reads_across_chunks(monkeypatch):
    monkeypatch.setattr(audio, "CHUNK_SIZE", 4096)
    data = os.urandom(4096 * 5 + 123)
    server = RangeServer(data)
    try:
        f = CdnFile(new_http_session(), [server.url])
        assert f.size == len(data)
        assert f.read_at(0, 10) == data[:10]
        assert f.read_at(4090, 20) == data[4090:4110]  # spans two chunks
        assert f.read_at(len(data) - 5, 100) == data[-5:]  # clipped at the end
        assert f.read_at(len(data), 10) == b""
        assert f.read_at(0, len(data)) == data
        assert f.complete
        f.close()
    finally:
        server.close()


def test_cdn_file_retries_transient_errors(monkeypatch):
    monkeypatch.setattr(audio, "CHUNK_SIZE", 4096)
    data = os.urandom(10000)
    server = RangeServer(data)
    server.fail_first = 1
    try:
        f = CdnFile(new_http_session(), [server.url])
        assert f.read_at(0, len(data)) == data
        f.close()
    finally:
        server.close()


def test_select_file_prefers_configured_bitrate_then_falls_back():
    def file(fmt):
        f = metadata_pb2.AudioFile()
        f.file_id = os.urandom(20)
        f.format = fmt
        return f

    files = [file(F.OGG_VORBIS_96), file(F.OGG_VORBIS_160), file(F.OGG_VORBIS_320), file(F.AAC_24)]
    assert select_file(files, 320).format == F.OGG_VORBIS_320
    assert select_file(files, 160).format == F.OGG_VORBIS_160
    assert select_file(files, 96).format == F.OGG_VORBIS_96
    assert select_file([file(F.OGG_VORBIS_96)], 320).format == F.OGG_VORBIS_96
    assert select_file([file(F.AAC_24), file(F.FLAC_FLAC)], 320) is None
    assert select_file([], 320) is None


def restriction(catalogue="premium", allowed=None, forbidden=None):
    r = metadata_pb2.Restriction()
    r.catalogue_str.append(catalogue)
    if allowed is not None:
        r.countries_allowed = allowed
    if forbidden is not None:
        r.countries_forbidden = forbidden
    return r


def test_country_restrictions():
    assert not is_restricted([], "NO")
    assert not is_restricted([restriction(allowed="SENODK")], "NO")
    assert is_restricted([restriction(allowed="SEDK")], "NO")
    assert is_restricted([restriction(allowed="")], "NO")
    assert is_restricted([restriction(forbidden="NOUS")], "NO")
    assert not is_restricted([restriction(forbidden="USGB")], "NO")
    # a restriction for another catalogue does not apply to this account
    assert not is_restricted([restriction(catalogue="free", allowed="US")], "NO")
    # unknown country: do not block
    assert not is_restricted([restriction(allowed="US")], "")


def test_normalisation_factor():
    assert normalisation_factor(0.0, 1.0) == pytest.approx(1.0)
    assert normalisation_factor(-6.0, 0.5) == pytest.approx(10 ** (-6 / 20))
    assert normalisation_factor(6.0, 0.25) == pytest.approx(1.0)  # never amplifies past full scale
    assert normalisation_factor(-1.0, 2.0) == pytest.approx(0.5)  # limited by the peak


class FakeSpClient:
    def __init__(self, track, audio_files, urls):
        self.track = track
        self.audio_files = audio_files
        self.urls = urls
        self.country = "NO"

    def extended_metadata(self, uri, kind, message):
        if kind == kind_pb.TRACK_V4:
            message.CopyFrom(self.track)
        elif kind == kind_pb.AUDIO_FILES:
            if self.audio_files is None:
                raise LookupError("none")
            message.CopyFrom(self.audio_files)
        else:
            raise LookupError("unsupported")
        return message

    def resolve_audio_storage(self, file_id):
        return self.urls


class FakeAp:
    country = "NO"
    attributes = {"catalogue": "premium"}

    def __init__(self, key):
        self.key = key
        self.requests = []

    def request_audio_key(self, gid, file_id):
        self.requests.append((gid, file_id))
        return self.key


def make_track(file_formats=(F.OGG_VORBIS_320,)):
    track = metadata_pb2.Track()
    track.gid = SpotifyId.from_uri(TRACK_URI).gid
    track.name = "Test Song"
    track.album.name = "Test Album"
    track.artist.add().name = "Test Artist"
    track.duration = 6000
    for fmt in file_formats:
        f = track.file.add()
        f.file_id = os.urandom(20)
        f.format = fmt
    return track


def test_loader_end_to_end_over_http(ogg, monkeypatch):
    monkeypatch.setattr(audio, "CHUNK_SIZE", 16 * 1024)
    key = os.urandom(16)
    server = RangeServer(spotify_file(ogg, key, track_gain_db=-6.0, track_peak=0.5))
    try:
        track_meta = make_track()
        ap = FakeAp(key)
        loader = AudioLoader(FakeSpClient(track_meta, None, [server.url]), ap, new_http_session(), 320)
        track = loader.load(TRACK_URI)
        assert track.info.name == "Test Song"
        assert track.info.artists == ["Test Artist"]
        assert track.info.duration_ms == 6000
        assert (track.info.codec, track.info.bitrate) == ("vorbis", 320)
        assert track.normalisation_gain == pytest.approx(10 ** (-6 / 20))
        assert ap.requests == [(track_meta.gid, track_meta.file[0].file_id)]

        expected = decode_all(io.BytesIO(ogg))
        got = []
        while True:
            block = track.read(4096)
            if block.shape[0] == 0:
                break
            got.append(block)
        assert track.error is None
        assert np.array_equal(np.concatenate(got), expected)
        assert track.position_ms == len(expected) * 1000 // SAMPLE_RATE
        track.close()
    finally:
        server.close()


def test_loader_starts_at_requested_position(ogg):
    key = os.urandom(16)
    server = RangeServer(spotify_file(ogg, key))
    try:
        loader = AudioLoader(FakeSpClient(make_track(), None, [server.url]), FakeAp(key), new_http_session())
        track = loader.load(TRACK_URI, position_ms=3000)
        assert track.position_ms == 3000
        expected = decode_all(io.BytesIO(ogg))[SAMPLE_RATE * 3 :]
        block = track.read(2048)
        assert np.allclose(block, expected[:2048], atol=1e-6)
        track.close()
    finally:
        server.close()


def test_loader_uses_audio_files_extension_when_track_lists_no_files(ogg):
    key = os.urandom(16)
    server = RangeServer(spotify_file(ogg, key))
    try:
        ext = audio_files_pb.AudioFilesExtensionResponse()
        entry = ext.files.add()
        entry.file.file_id = os.urandom(20)
        entry.file.format = F.OGG_VORBIS_160
        loader = AudioLoader(FakeSpClient(make_track(()), ext, [server.url]), FakeAp(key), new_http_session())
        track = loader.load(TRACK_URI)
        assert track.info.bitrate == 160
        track.close()
    finally:
        server.close()


def test_loader_reports_unavailable_cases():
    key = os.urandom(16)
    http = new_http_session()

    restricted = make_track()
    restricted.restriction.append(restriction(allowed="US"))
    with pytest.raises(Unavailable):
        AudioLoader(FakeSpClient(restricted, None, []), FakeAp(key), http).load(TRACK_URI)

    with pytest.raises(Unavailable):
        AudioLoader(FakeSpClient(make_track(()), None, []), FakeAp(key), http).load(TRACK_URI)

    with pytest.raises(Unavailable):
        AudioLoader(FakeSpClient(make_track(), None, []), FakeAp(key), http).load("spotify:album:4uLU6hMCjMI75M1A2tKUQC")

    with pytest.raises(Unavailable):
        AudioLoader(FakeSpClient(make_track(), None, []), FakeAp(key), http).load("not a uri")


def test_loader_plays_alternative_when_track_is_restricted(ogg):
    key = os.urandom(16)
    server = RangeServer(spotify_file(ogg, key))
    try:
        track_meta = make_track()
        track_meta.restriction.append(restriction(allowed="US"))
        alt = track_meta.alternative.add()
        alt.gid = os.urandom(16)
        f = alt.file.add()
        f.file_id = os.urandom(20)
        f.format = F.OGG_VORBIS_320
        ap = FakeAp(key)
        loader = AudioLoader(FakeSpClient(track_meta, None, [server.url]), ap, new_http_session())
        track = loader.load(TRACK_URI)
        assert ap.requests == [(alt.gid, f.file_id)]  # the key is requested for the alternative
        assert track.info.uri == TRACK_URI  # but it is still reported as the requested track
        track.close()
    finally:
        server.close()
