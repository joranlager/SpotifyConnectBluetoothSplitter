//! Child-process helpers: lookup, log files, process groups, graceful shutdown, backoff.

use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

/// Find an executable: a path as given, or a name looked up on `PATH`.
pub fn which(name: &str) -> Option<PathBuf> {
    let path = Path::new(name);
    if path.components().count() > 1 {
        return path.is_file().then(|| path.to_path_buf());
    }
    let paths = std::env::var_os("PATH")?;
    std::env::split_paths(&paths).map(|dir| dir.join(name)).find(|c| c.is_file())
}

/// Open `<dir>/<name>.log` for appending, creating the directory if needed.
pub fn open_log(dir: &Path, name: &str) -> std::io::Result<File> {
    std::fs::create_dir_all(dir)?;
    OpenOptions::new().create(true).append(true).open(dir.join(format!("{name}.log")))
}

/// Spawn a child in its own process group so that the whole group (Spotify is
/// multi-process) can be signalled and cleaned up together.
pub fn spawn_group(cmd: &mut Command) -> std::io::Result<Child> {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    cmd.spawn()
}

#[cfg(unix)]
fn signal_group(pid: u32, kill: bool) {
    let sig = if kill { libc::SIGKILL } else { libc::SIGTERM };
    // SAFETY: kill(2) with a negative pid signals a process group; no memory is touched.
    unsafe {
        libc::kill(-(pid as libc::pid_t), sig);
    }
}

#[cfg(not(unix))]
fn signal_group(_pid: u32, _kill: bool) {}

/// A supervised child. Dropping it stops the child (SIGTERM, then SIGKILL) and any
/// processes it left in its group.
pub struct Managed {
    name: &'static str,
    child: Child,
    done: bool,
}

impl Managed {
    pub fn new(name: &'static str, child: Child) -> Self {
        Managed { name, child, done: false }
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// The exit status if the child has exited, without blocking.
    pub fn poll(&mut self) -> Option<ExitStatus> {
        self.child.try_wait().ok().flatten()
    }

    pub fn terminate(&mut self) {
        if self.done {
            return;
        }
        self.done = true;
        let pid = self.child.id();
        if matches!(self.child.try_wait(), Ok(None)) {
            signal_group(pid, false);
            let deadline = Instant::now() + Duration::from_secs(5);
            while Instant::now() < deadline && matches!(self.child.try_wait(), Ok(None)) {
                thread::sleep(Duration::from_millis(50));
            }
            if matches!(self.child.try_wait(), Ok(None)) {
                log_warn!("{} did not stop, killing it", self.name);
                signal_group(pid, true);
                let _ = self.child.kill();
                let _ = self.child.wait();
            }
        }
        // Anything the child left behind in its group.
        signal_group(pid, true);
    }
}

impl Drop for Managed {
    fn drop(&mut self) {
        self.terminate();
    }
}

/// Sleep `total`, waking early when `stop` is set. Returns false if stopped.
pub fn sleep_unless_stopped(total: Duration, stop: &AtomicBool) -> bool {
    let end = Instant::now() + total;
    while Instant::now() < end {
        if stop.load(Ordering::SeqCst) {
            return false;
        }
        thread::sleep(Duration::from_millis(100).min(end.saturating_duration_since(Instant::now())));
    }
    !stop.load(Ordering::SeqCst)
}

/// Exponential restart delay: 1s, 2s, 4s ... capped at 30s, and reset once a run
/// lasted at least a minute.
pub struct Backoff {
    delay: Duration,
}

impl Backoff {
    const MIN: Duration = Duration::from_secs(1);
    const MAX: Duration = Duration::from_secs(30);
    const STABLE: Duration = Duration::from_secs(60);

    pub fn new() -> Self {
        Backoff { delay: Self::MIN }
    }

    /// The delay before the next restart, given how long the last run lasted.
    pub fn next(&mut self, ran_for: Duration) -> Duration {
        if ran_for >= Self::STABLE {
            self.delay = Self::MIN;
        }
        let now = self.delay;
        self.delay = (self.delay * 2).min(Self::MAX);
        now
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_doubles_caps_and_resets() {
        let mut b = Backoff::new();
        let short = Duration::from_secs(1);
        let secs: Vec<u64> = (0..7).map(|_| b.next(short).as_secs()).collect();
        assert_eq!(secs, vec![1, 2, 4, 8, 16, 30, 30]);
        assert_eq!(b.next(Duration::from_secs(120)).as_secs(), 1);
        assert_eq!(b.next(short).as_secs(), 2);
    }

    #[test]
    fn which_finds_nothing_for_missing_name() {
        assert!(which("definitely-not-a-real-binary-xyz").is_none());
        assert!(which("/definitely/not/here").is_none());
    }

    #[test]
    fn sleep_returns_early_when_stopped() {
        let stop = AtomicBool::new(true);
        let started = Instant::now();
        assert!(!sleep_unless_stopped(Duration::from_secs(10), &stop));
        assert!(started.elapsed() < Duration::from_secs(1));
    }
}
