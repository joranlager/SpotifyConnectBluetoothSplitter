//! Capture the sink's monitor with `parec` and hand the raw PCM to a reader.
//!
//! Output is signed 16-bit little-endian, 2 channels, at the configured rate, the
//! same format the splitter fans out. With a FIFO target the capture waits for a
//! reader, and starts again whenever the reader goes away and a new one appears.

use crate::cli::Target;
use crate::proc::{self, Backoff, Managed};
use crate::Result;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub struct CaptureConfig {
    pub source: String,
    pub rate: u32,
    pub target: Target,
    pub log_dir: PathBuf,
}

/// Make sure a FIFO target exists (creating it), before anything is started.
pub fn prepare(target: &Target) -> Result<()> {
    match target {
        Target::Stdout => Ok(()),
        Target::Fifo(path) => ensure_fifo(path),
    }
}

#[cfg(unix)]
fn ensure_fifo(path: &Path) -> Result<()> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::FileTypeExt;
    match std::fs::metadata(path) {
        Ok(meta) if meta.file_type().is_fifo() => return Ok(()),
        Ok(_) => return Err(format!("{} exists and is not a FIFO", path.display()).into()),
        Err(_) => {}
    }
    let c_path = CString::new(path.as_os_str().as_bytes()).map_err(|_| "FIFO path contains a NUL byte")?;
    // SAFETY: c_path is a valid NUL-terminated string that outlives the call.
    if unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) } != 0 {
        return Err(format!("could not create FIFO {}: {}", path.display(), std::io::Error::last_os_error()).into());
    }
    Ok(())
}

#[cfg(not(unix))]
fn ensure_fifo(_path: &Path) -> Result<()> {
    Err("FIFO output needs Unix".into())
}

/// Open the FIFO for writing, polling until a reader exists (a plain blocking open
/// could not be interrupted). `None` means we were asked to stop.
#[cfg(unix)]
fn open_fifo(path: &Path, stop: &AtomicBool) -> Result<Option<std::fs::File>> {
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::OpenOptionsExt;
    let mut announced = false;
    loop {
        if stop.load(Ordering::SeqCst) {
            return Ok(None);
        }
        match std::fs::OpenOptions::new().write(true).custom_flags(libc::O_NONBLOCK).open(path) {
            Ok(file) => {
                // parec needs ordinary blocking writes.
                // SAFETY: fcntl on a file descriptor we own, with plain flag arguments.
                unsafe {
                    let fd = file.as_raw_fd();
                    let flags = libc::fcntl(fd, libc::F_GETFL);
                    libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
                }
                return Ok(Some(file));
            }
            Err(e) if e.raw_os_error() == Some(libc::ENXIO) => {
                if !announced {
                    log_info!("waiting for a reader on {}", path.display());
                    announced = true;
                }
                thread::sleep(Duration::from_millis(200));
            }
            Err(e) => return Err(format!("could not open {}: {e}", path.display()).into()),
        }
    }
}

#[cfg(not(unix))]
fn open_fifo(_path: &Path, _stop: &AtomicBool) -> Result<Option<std::fs::File>> {
    Err("FIFO output needs Unix".into())
}

pub fn spawn(cfg: CaptureConfig, stop: Arc<AtomicBool>) -> JoinHandle<()> {
    thread::spawn(move || {
        let mut backoff = Backoff::new();
        while !stop.load(Ordering::SeqCst) {
            let started = Instant::now();
            if let Err(e) = run_once(&cfg, &stop) {
                log_warn!("capture: {e}");
            }
            if stop.load(Ordering::SeqCst) {
                break;
            }
            if cfg.target == Target::Stdout {
                // Nothing can reconnect to a closed stdout pipe: shut the whole app down.
                log_warn!("capture to stdout ended, shutting down");
                stop.store(true, Ordering::SeqCst);
                break;
            }
            let delay = backoff.next(started.elapsed());
            if !proc::sleep_unless_stopped(delay, &stop) {
                break;
            }
        }
    })
}

fn run_once(cfg: &CaptureConfig, stop: &AtomicBool) -> Result<()> {
    let out: Stdio = match &cfg.target {
        Target::Stdout => Stdio::inherit(),
        Target::Fifo(path) => match open_fifo(path, stop)? {
            Some(file) => file.into(),
            None => return Ok(()),
        },
    };
    let parec = proc::which("parec").ok_or("parec not found (install 'pulseaudio-utils')")?;
    let log = proc::open_log(&cfg.log_dir, "parec")?;
    let mut cmd = Command::new(parec);
    cmd.arg(format!("--device={}", cfg.source))
        .args(["--format=s16le", "--channels=2", "--client-name=spotify-headless"])
        .arg(format!("--rate={}", cfg.rate))
        .arg("--latency-msec=20")
        .stdin(Stdio::null())
        .stdout(out)
        .stderr(log);
    let mut child = Managed::new("parec", proc::spawn_group(&mut cmd).map_err(|e| format!("could not start parec: {e}"))?);
    log_info!("capturing {} ({} Hz, s16le, stereo)", cfg.source, cfg.rate);
    loop {
        if stop.load(Ordering::SeqCst) {
            return Ok(());
        }
        if let Some(status) = child.poll() {
            log_info!("parec exited ({status})");
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }
}
