//! The virtual sink, via `pactl` (works with PulseAudio and with PipeWire's pulse layer).

use crate::Result;
use std::process::Command;

fn pactl(args: &[&str]) -> Result<String> {
    // LC_ALL=C: the long listings are localised otherwise, and we parse them.
    let out = Command::new("pactl")
        .env("LC_ALL", "C")
        .args(args)
        .output()
        .map_err(|e| format!("could not run pactl: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "pactl {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        )
        .into());
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Fails with a helpful message when no PulseAudio/PipeWire server is reachable.
pub fn check_server() -> Result<String> {
    let info = pactl(&["info"]).map_err(|e| {
        format!("no PulseAudio/PipeWire server reachable ({e}). Run this inside the user's audio session (XDG_RUNTIME_DIR set).")
    })?;
    let name = info
        .lines()
        .find_map(|l| l.strip_prefix("Server Name:"))
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|| "unknown".into());
    Ok(name)
}

/// The sink we created (and so must remove on exit), or one that already existed.
pub struct SinkGuard {
    pub name: String,
    module: Option<u32>,
}

impl SinkGuard {
    pub fn monitor_source(&self) -> String {
        format!("{}.monitor", self.name)
    }
}

impl Drop for SinkGuard {
    fn drop(&mut self) {
        if let Some(module) = self.module.take() {
            if let Err(e) = pactl(&["unload-module", &module.to_string()]) {
                log_warn!("could not remove sink {}: {e}", self.name);
            }
        }
    }
}

/// Create the null sink, or reuse one with that name.
pub fn ensure_null_sink(name: &str, rate: u32) -> Result<SinkGuard> {
    let sinks = parse_sinks(&pactl(&["list", "short", "sinks"])?);
    if sinks.iter().any(|(_, n)| n == name) {
        log_info!("audio sink '{name}' already exists, reusing it");
        return Ok(SinkGuard { name: name.to_string(), module: None });
    }
    let out = pactl(&[
        "load-module",
        "module-null-sink",
        &format!("sink_name={name}"),
        &format!("sink_properties=device.description={name}"),
        &format!("rate={rate}"),
        "channels=2",
        "format=s16le",
    ])?;
    let module = out
        .trim()
        .parse::<u32>()
        .map_err(|_| format!("unexpected output from pactl load-module: '{}'", out.trim()))?;
    Ok(SinkGuard { name: name.to_string(), module: Some(module) })
}

/// `pactl list short sinks` -> (index, name).
pub fn parse_sinks(text: &str) -> Vec<(u32, String)> {
    text.lines()
        .filter_map(|line| {
            let mut cols = line.split('\t');
            let id = cols.next()?.trim().parse().ok()?;
            let name = cols.next()?.trim().to_string();
            Some((id, name))
        })
        .collect()
}

#[derive(Debug, PartialEq)]
pub struct SinkInput {
    pub id: u32,
    pub sink: u32,
    pub app_name: String,
    pub binary: String,
}

fn unquote(value: &str) -> String {
    value.trim().trim_matches('"').to_string()
}

/// `pactl list sink-inputs` -> the playback streams.
pub fn parse_sink_inputs(text: &str) -> Vec<SinkInput> {
    let mut inputs: Vec<SinkInput> = Vec::new();
    for raw in text.lines() {
        let line = raw.trim();
        if let Some(id) = line.strip_prefix("Sink Input #") {
            if let Ok(id) = id.trim().parse() {
                inputs.push(SinkInput { id, sink: u32::MAX, app_name: String::new(), binary: String::new() });
            }
            continue;
        }
        let Some(current) = inputs.last_mut() else { continue };
        if let Some(sink) = line.strip_prefix("Sink:") {
            if let Ok(sink) = sink.trim().parse() {
                current.sink = sink;
            }
        } else if let Some(v) = line.strip_prefix("application.name = ") {
            current.app_name = unquote(v);
        } else if let Some(v) = line.strip_prefix("application.process.binary = ") {
            current.binary = unquote(v);
        }
    }
    inputs
}

fn is_client(input: &SinkInput, binary: &str) -> bool {
    let wanted = binary.to_ascii_lowercase();
    input.binary.eq_ignore_ascii_case(binary) || input.app_name.to_ascii_lowercase().contains(&wanted)
}

/// The streams of `binary` that are not yet on the target sink.
pub fn streams_to_move(inputs: &[SinkInput], target_sink: u32, binary: &str) -> Vec<u32> {
    inputs
        .iter()
        .filter(|i| is_client(i, binary) && i.sink != target_sink)
        .map(|i| i.id)
        .collect()
}

/// Move any playback stream of the Spotify client that ended up on another sink
/// (PULSE_SINK is normally enough; this catches streams that bypass it).
pub fn reroute(sink_name: &str, client_binary: &str) -> Result<usize> {
    let sinks = parse_sinks(&pactl(&["list", "short", "sinks"])?);
    let target = sinks
        .iter()
        .find(|(_, n)| n == sink_name)
        .map(|(i, _)| *i)
        .ok_or_else(|| format!("sink '{sink_name}' is gone"))?;
    let inputs = parse_sink_inputs(&pactl(&["list", "sink-inputs"])?);
    let ids = streams_to_move(&inputs, target, client_binary);
    for id in &ids {
        pactl(&["move-sink-input", &id.to_string(), sink_name])?;
    }
    Ok(ids.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    const SINK_INPUTS: &str = "\
Sink Input #41
\tDriver: protocol-native.c
\tOwner Module: 10
\tClient: 55
\tSink: 3
\tProperties:
\t\tapplication.name = \"Chromium\"
\t\tapplication.process.binary = \"firefox\"
Sink Input #42
\tDriver: PipeWire
\tSink: 7
\tProperties:
\t\tapplication.name = \"spotify\"
\t\tapplication.process.binary = \"spotify\"
Sink Input #43
\tSink: 9
\tProperties:
\t\tapplication.name = \"Spotify\"
";

    #[test]
    fn parses_short_sinks() {
        let text = "3\talsa_output.pci\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n7\tspotify_headless\tPipeWire\ts16le 2ch 44100Hz\tIDLE\n";
        assert_eq!(
            parse_sinks(text),
            vec![(3, "alsa_output.pci".to_string()), (7, "spotify_headless".to_string())]
        );
        assert!(parse_sinks("").is_empty());
    }

    #[test]
    fn parses_sink_inputs() {
        let inputs = parse_sink_inputs(SINK_INPUTS);
        assert_eq!(inputs.len(), 3);
        assert_eq!(inputs[1], SinkInput { id: 42, sink: 7, app_name: "spotify".into(), binary: "spotify".into() });
        assert_eq!(inputs[2].app_name, "Spotify");
        assert_eq!(inputs[2].binary, "");
    }

    #[test]
    fn moves_only_spotify_streams_that_are_elsewhere() {
        let inputs = parse_sink_inputs(SINK_INPUTS);
        // Target sink 7: #42 is already there, #43 (matched by app name) is not, #41 is Firefox.
        assert_eq!(streams_to_move(&inputs, 7, "spotify"), vec![43]);
        assert!(streams_to_move(&inputs, 9, "spotify").contains(&42));
    }
}
