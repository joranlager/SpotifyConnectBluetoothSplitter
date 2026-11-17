//! Command-line parsing (no dependencies).

use std::path::PathBuf;

pub const USAGE: &str = "\
spotify-headless: run the official Spotify desktop client headless on Linux

USAGE:
    spotify-headless [run] [OPTIONS]
    spotify-headless doctor [OPTIONS]
    spotify-headless help

OPTIONS:
    --sink-name NAME      Virtual audio sink to create/use      [default: spotify_headless]
    --rate HZ             Sample rate of the sink and capture   [default: 44100]
    --pcm-out TARGET      Write the sink's audio as raw PCM (s16le, stereo) to TARGET:
                          '-' for stdout, or a FIFO path (created if missing).
                          Without it nothing is captured; read '<sink>.monitor' yourself.
    --vnc-port PORT       Also run x11vnc on 127.0.0.1:PORT, to sign in once
    --resolution WxHxD    Virtual display size                  [default: 1280x720x24]
    --display-start N     First X display number to try         [default: 99]
    --spotify-bin PATH    Spotify executable                    [default: spotify]
    --spotify-arg ARG     Extra argument for Spotify (repeatable)
    --data-dir DIR        Keep Spotify's config and cache in DIR (sets XDG_CONFIG_HOME
                          and XDG_CACHE_HOME for the client) instead of the user's own
    --log-dir DIR         Where Xvfb/Spotify/parec logs go
    -h, --help            Show this help
";

#[derive(Debug, Clone, PartialEq)]
pub enum Target {
    Stdout,
    Fifo(PathBuf),
}

#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub sink_name: String,
    pub rate: u32,
    pub resolution: String,
    pub display_start: u32,
    pub spotify_bin: String,
    pub spotify_args: Vec<String>,
    pub pcm_out: Option<Target>,
    pub vnc_port: Option<u16>,
    pub data_dir: Option<PathBuf>,
    pub log_dir: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Action {
    Run,
    Doctor,
    Help,
}

fn default_log_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("XDG_STATE_HOME").filter(|v| !v.is_empty()) {
        return PathBuf::from(dir).join("spotify-headless");
    }
    if let Some(home) = std::env::var_os("HOME").filter(|v| !v.is_empty()) {
        return PathBuf::from(home).join(".local/state/spotify-headless");
    }
    PathBuf::from("/tmp/spotify-headless")
}

impl Config {
    pub fn defaults() -> Self {
        Config {
            sink_name: "spotify_headless".into(),
            rate: 44100,
            resolution: "1280x720x24".into(),
            display_start: 99,
            spotify_bin: "spotify".into(),
            spotify_args: Vec::new(),
            pcm_out: None,
            vnc_port: None,
            data_dir: None,
            log_dir: default_log_dir(),
        }
    }
}

fn valid_sink_name(name: &str) -> bool {
    !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn valid_resolution(text: &str) -> bool {
    let parts: Vec<&str> = text.split('x').collect();
    parts.len() == 3 && parts.iter().all(|p| p.parse::<u32>().map(|n| n > 0).unwrap_or(false))
}

pub fn parse<I: IntoIterator<Item = String>>(args: I) -> Result<(Action, Config), String> {
    let mut it = args.into_iter().peekable();
    let mut action = Action::Run;
    if let Some(first) = it.peek() {
        let sub = match first.as_str() {
            "run" => Some(Action::Run),
            "doctor" => Some(Action::Doctor),
            "help" => Some(Action::Help),
            _ => None,
        };
        if let Some(sub) = sub {
            action = sub;
            it.next();
        }
    }

    let mut cfg = Config::defaults();
    while let Some(arg) = it.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((f, v)) if f.starts_with("--") => (f.to_string(), Some(v.to_string())),
            _ => (arg.clone(), None),
        };
        let mut value = |name: &str| -> Result<String, String> {
            inline.clone().or_else(|| it.next()).ok_or_else(|| format!("{name} needs a value"))
        };
        match flag.as_str() {
            "-h" | "--help" => action = Action::Help,
            "--sink-name" => {
                let v = value("--sink-name")?;
                if !valid_sink_name(&v) {
                    return Err("--sink-name may only contain letters, digits, '_' and '-'".into());
                }
                cfg.sink_name = v;
            }
            "--rate" => {
                let v = value("--rate")?;
                let rate: u32 = v.parse().map_err(|_| format!("--rate: '{v}' is not a number"))?;
                if !(8000..=192_000).contains(&rate) {
                    return Err("--rate must be between 8000 and 192000".into());
                }
                cfg.rate = rate;
            }
            "--resolution" => {
                let v = value("--resolution")?;
                if !valid_resolution(&v) {
                    return Err("--resolution must look like 1280x720x24".into());
                }
                cfg.resolution = v;
            }
            "--display-start" => {
                let v = value("--display-start")?;
                cfg.display_start = v.parse().map_err(|_| format!("--display-start: '{v}' is not a number"))?;
            }
            "--spotify-bin" => cfg.spotify_bin = value("--spotify-bin")?,
            "--spotify-arg" => cfg.spotify_args.push(value("--spotify-arg")?),
            "--pcm-out" => {
                let v = value("--pcm-out")?;
                cfg.pcm_out = Some(if v == "-" { Target::Stdout } else { Target::Fifo(PathBuf::from(v)) });
            }
            "--vnc-port" => {
                let v = value("--vnc-port")?;
                cfg.vnc_port = Some(v.parse().map_err(|_| format!("--vnc-port: '{v}' is not a port"))?);
            }
            "--data-dir" => cfg.data_dir = Some(PathBuf::from(value("--data-dir")?)),
            "--log-dir" => cfg.log_dir = PathBuf::from(value("--log-dir")?),
            other => return Err(format!("unknown option '{other}' (try --help)")),
        }
    }
    Ok((action, cfg))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(args: &[&str]) -> Result<(Action, Config), String> {
        parse(args.iter().map(|s| s.to_string()))
    }

    #[test]
    fn defaults_run() {
        let (action, cfg) = p(&[]).unwrap();
        assert_eq!(action, Action::Run);
        assert_eq!(cfg.sink_name, "spotify_headless");
        assert_eq!(cfg.pcm_out, None);
    }

    #[test]
    fn subcommands() {
        assert_eq!(p(&["doctor"]).unwrap().0, Action::Doctor);
        assert_eq!(p(&["help"]).unwrap().0, Action::Help);
        assert_eq!(p(&["--help"]).unwrap().0, Action::Help);
        assert_eq!(p(&["run", "--rate", "48000"]).unwrap().1.rate, 48000);
    }

    #[test]
    fn value_forms() {
        let (_, a) = p(&["--sink-name", "x_y"]).unwrap();
        let (_, b) = p(&["--sink-name=x_y"]).unwrap();
        assert_eq!(a.sink_name, "x_y");
        assert_eq!(a, b);
    }

    #[test]
    fn pcm_out_targets() {
        assert_eq!(p(&["--pcm-out", "-"]).unwrap().1.pcm_out, Some(Target::Stdout));
        assert_eq!(
            p(&["--pcm-out=/tmp/a.fifo"]).unwrap().1.pcm_out,
            Some(Target::Fifo(PathBuf::from("/tmp/a.fifo")))
        );
    }

    #[test]
    fn repeated_spotify_args() {
        let (_, cfg) = p(&["--spotify-arg", "--a", "--spotify-arg=--b"]).unwrap();
        assert_eq!(cfg.spotify_args, vec!["--a", "--b"]);
    }

    #[test]
    fn rejects_bad_input() {
        assert!(p(&["--sink-name", "has space"]).is_err());
        assert!(p(&["--rate", "12"]).is_err());
        assert!(p(&["--rate"]).is_err());
        assert!(p(&["--resolution", "1280x720"]).is_err());
        assert!(p(&["--vnc-port", "99999"]).is_err());
        assert!(p(&["--nope"]).is_err());
    }
}
