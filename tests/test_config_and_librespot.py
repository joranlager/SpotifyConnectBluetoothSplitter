import pytest

from splitter.config import AppConfig, parse_config
from splitter.librespot import build_command


def test_parse_minimal_config_uses_defaults():
    cfg = parse_config({"speakers": [{"match": "JBL"}]})
    assert cfg.bitrate == 320
    assert cfg.speakers[0].match == "JBL"
    assert cfg.speakers[0].delay_ms == 0


def test_bad_bitrate_rejected():
    with pytest.raises(ValueError):
        parse_config({"bitrate": 128})


def test_empty_speaker_match_rejected():
    with pytest.raises(ValueError):
        parse_config({"speakers": [{"match": "  "}]})


def test_delay_out_of_range_rejected():
    with pytest.raises(ValueError):
        parse_config({"speakers": [{"match": "x", "delay_ms": 9000}]})


def test_build_command_uses_pipe_backend_and_extra_args():
    cfg = AppConfig(device_name="Test", cache_dir="c", librespot_args=["--verbose"])
    cmd = build_command(cfg)
    assert cmd[0] == "librespot"
    assert cmd[cmd.index("--backend") + 1] == "pipe"
    assert cmd[cmd.index("--device") + 1] == "-"
    assert cmd[cmd.index("--name") + 1] == "Test"
    assert cmd[cmd.index("--cache") + 1] == "c"
    assert cmd[-1] == "--verbose"


def test_receiver_defaults_to_builtin_and_is_validated():
    assert parse_config({}).receiver == "builtin"
    assert parse_config({"receiver": "librespot"}).receiver == "librespot"
    with pytest.raises(ValueError):
        parse_config({"receiver": "go-librespot"})
