import pytest

from splitter.config import (
    DEFAULT_RECEIVER,
    VALID_RECEIVERS,
    AppConfig,
    config_to_dict,
    parse_config,
)


def test_parse_minimal_config_uses_defaults():
    cfg = parse_config({"speakers": [{"match": "JBL"}]})
    assert cfg.receiver == DEFAULT_RECEIVER
    assert cfg.buffer_seconds == 2.0
    assert cfg.speakers[0].match == "JBL"
    assert cfg.speakers[0].delay_ms == 0


def test_empty_speaker_match_rejected():
    with pytest.raises(ValueError):
        parse_config({"speakers": [{"match": "  "}]})


def test_delay_out_of_range_rejected():
    with pytest.raises(ValueError):
        parse_config({"speakers": [{"match": "x", "delay_ms": 9000}]})


def test_only_spotifys_own_clients_are_valid_receivers():
    assert set(VALID_RECEIVERS) == {"soloist", "headless"}
    assert DEFAULT_RECEIVER in VALID_RECEIVERS
    for receiver in VALID_RECEIVERS:
        assert parse_config({"receiver": receiver}).receiver == receiver


@pytest.mark.parametrize("removed", ["builtin", "librespot"])
def test_removed_receivers_are_refused_with_an_explanation(removed):
    with pytest.raises(ValueError, match="no longer supported"):
        parse_config({"receiver": removed})


def test_unknown_receiver_is_refused():
    with pytest.raises(ValueError, match="receiver must be one of"):
        parse_config({"receiver": "go-librespot"})


def test_keys_of_removed_receivers_are_ignored_not_kept():
    old = {"receiver": "soloist", "bitrate": 320, "cache_dir": ".librespot-cache", "librespot_path": "librespot"}
    cfg = parse_config(old)
    saved = config_to_dict(cfg)
    assert not {"bitrate", "cache_dir", "librespot_path", "librespot_args"} & set(saved)
    assert parse_config(saved) == cfg


def test_config_has_no_trace_of_the_removed_receivers():
    assert not any("librespot" in key or "spconnect" in key for key in config_to_dict(AppConfig()))
