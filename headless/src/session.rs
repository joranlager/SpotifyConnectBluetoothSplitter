//! The supervisor: builds the sink once, then keeps display + Spotify running.

use crate::audio::{self, SinkGuard};
use crate::capture::{self, CaptureConfig};
use crate::cli::Config;
use crate::proc::{self, Backoff};
use crate::{spotify, xserver, Result};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

pub fn run(cfg: &Config, stop: Arc<AtomicBool>) -> Result<()> {
    let spotify_exe = proc::which(&cfg.spotify_bin)
        .ok_or_else(|| format!("Spotify not found ('{}'); install it or pass --spotify-bin", cfg.spotify_bin))?;
    proc::which("Xvfb").ok_or("Xvfb not found (install the 'xvfb' package)")?;
    if cfg.vnc_port.is_some() {
        proc::which("x11vnc").ok_or("x11vnc not found (install the 'x11vnc' package)")?;
    }
    if let Some(target) = &cfg.pcm_out {
        capture::prepare(target)?;
    }
    let server = audio::check_server()?;
    let sink = audio::ensure_null_sink(&cfg.sink_name, cfg.rate)?;
    log_info!("audio server: {server}; sink '{}' ready, monitor source: {}", sink.name, sink.monitor_source());
    log_info!("logs are in {}", cfg.log_dir.display());

    let capture = cfg.pcm_out.clone().map(|target| {
        capture::spawn(
            CaptureConfig { source: sink.monitor_source(), rate: cfg.rate, target, log_dir: cfg.log_dir.clone() },
            Arc::clone(&stop),
        )
    });

    let mut backoff = Backoff::new();
    while !stop.load(Ordering::SeqCst) {
        let started = Instant::now();
        if let Err(e) = run_session(cfg, &stop, &spotify_exe, &sink) {
            log_warn!("{e}");
        }
        if stop.load(Ordering::SeqCst) {
            break;
        }
        let delay = backoff.next(started.elapsed());
        log_info!("restarting in {}s", delay.as_secs());
        if !proc::sleep_unless_stopped(delay, &stop) {
            break;
        }
    }

    log_info!("shutting down");
    stop.store(true, Ordering::SeqCst);
    if let Some(handle) = capture {
        let _ = handle.join();
    }
    Ok(()) // `sink` is dropped here and removes the virtual sink
}

/// One display + Spotify lifetime. Returns when either dies or `stop` is set; the
/// children are stopped in reverse order of creation when the locals drop.
fn run_session(cfg: &Config, stop: &AtomicBool, spotify_exe: &Path, sink: &SinkGuard) -> Result<()> {
    let mut display = xserver::start(cfg)?;
    log_info!("virtual display {} up ({})", display.display_str(), cfg.resolution);

    let mut vnc = match cfg.vnc_port {
        Some(port) => {
            let vnc = xserver::start_vnc(cfg, &display, port)?;
            log_info!("VNC on 127.0.0.1:{port} (tunnel it, e.g. ssh -L {port}:127.0.0.1:{port} <this-host>)");
            Some(vnc)
        }
        None => None,
    };

    let mut client = spotify::spawn(cfg, spotify_exe, &display, &sink.name)?;
    log_info!("Spotify started (pid {}); it appears in Spotify's device list once signed in", client.pid());

    let client_name = spotify_exe.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "spotify".into());
    let begun = Instant::now();
    let mut tick = 0u32;
    loop {
        if stop.load(Ordering::SeqCst) {
            return Ok(());
        }
        if let Some(status) = display.exited() {
            return Err(format!("Xvfb exited ({status}); see {}", cfg.log_dir.join("xvfb.log").display()).into());
        }
        if let Some(status) = client.poll() {
            return Err(format!(
                "Spotify exited ({status}) after {}s; see {}",
                begun.elapsed().as_secs(),
                cfg.log_dir.join("spotify.log").display()
            )
            .into());
        }
        if let Some(process) = vnc.as_mut() {
            if process.poll().is_some() {
                log_warn!("x11vnc exited; see {}", cfg.log_dir.join("x11vnc.log").display());
                vnc = None;
            }
        }
        if tick.is_multiple_of(4) {
            if let Ok(moved) = audio::reroute(&sink.name, &client_name) {
                if moved > 0 {
                    log_info!("moved {moved} Spotify stream(s) to '{}'", sink.name);
                }
            }
        }
        tick = tick.wrapping_add(1);
        thread::sleep(Duration::from_millis(500));
    }
}
