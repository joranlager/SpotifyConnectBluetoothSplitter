//! Launching the official Spotify client on the virtual display and sink.

use crate::cli::Config;
use crate::proc::{self, Managed};
use crate::xserver::XServer;
use crate::Result;
use std::path::Path;
use std::process::{Command, Stdio};

pub fn spawn(cfg: &Config, exe: &Path, display: &XServer, sink: &str) -> Result<Managed> {
    let log = proc::open_log(&cfg.log_dir, "spotify")?;
    let log_err = log.try_clone()?;
    let mut cmd = Command::new(exe);
    cmd.args(&cfg.spotify_args)
        .env("DISPLAY", display.display_str())
        .env("XAUTHORITY", &display.auth_file)
        // libpulse sends every stream without an explicit device to this sink.
        .env("PULSE_SINK", sink)
        // No GPU on the virtual display.
        .env("LIBGL_ALWAYS_SOFTWARE", "1")
        .env("XDG_SESSION_TYPE", "x11")
        .env_remove("WAYLAND_DISPLAY")
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(log_err);
    if let Some(dir) = &cfg.data_dir {
        let config = dir.join("config");
        let cache = dir.join("cache");
        std::fs::create_dir_all(&config).and_then(|_| std::fs::create_dir_all(&cache))?;
        cmd.env("XDG_CONFIG_HOME", config).env("XDG_CACHE_HOME", cache);
    }
    let child = proc::spawn_group(&mut cmd).map_err(|e| format!("could not start {}: {e}", exe.display()))?;
    Ok(Managed::new("spotify", child))
}
