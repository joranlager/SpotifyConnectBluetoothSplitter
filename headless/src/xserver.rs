//! The virtual X display (Xvfb), with a private auth cookie instead of `-ac`.

use crate::cli::Config;
use crate::proc::{self, Managed};
use crate::Result;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub struct XServer {
    pub display: u32,
    pub auth_file: PathBuf,
    proc: Managed,
}

impl XServer {
    pub fn display_str(&self) -> String {
        format!(":{}", self.display)
    }

    pub fn exited(&mut self) -> Option<ExitStatus> {
        self.proc.poll()
    }
}

impl Drop for XServer {
    fn drop(&mut self) {
        self.proc.terminate();
        let _ = fs::remove_file(&self.auth_file);
    }
}

fn socket_path(display: u32) -> PathBuf {
    PathBuf::from(format!("/tmp/.X11-unix/X{display}"))
}

fn display_taken(display: u32) -> bool {
    socket_path(display).exists() || Path::new(&format!("/tmp/.X{display}-lock")).exists()
}

/// The first display number from `start` (up to 100 of them) that `taken` says is free.
pub fn pick_display(start: u32, taken: impl Fn(u32) -> bool) -> Option<u32> {
    (start..start.saturating_add(100)).find(|n| !taken(*n))
}

fn push_field(out: &mut Vec<u8>, data: &[u8]) {
    out.extend_from_slice(&(data.len() as u16).to_be_bytes());
    out.extend_from_slice(data);
}

/// One Xauthority entry (FamilyLocal, MIT-MAGIC-COOKIE-1), the same thing
/// `xauth add :N . <cookie>` writes and `xvfb-run` uses.
pub fn xauth_entry(hostname: &str, display: u32, cookie: &[u8; 16]) -> Vec<u8> {
    const FAMILY_LOCAL: u16 = 256;
    let mut out = Vec::new();
    out.extend_from_slice(&FAMILY_LOCAL.to_be_bytes());
    push_field(&mut out, hostname.as_bytes());
    push_field(&mut out, display.to_string().as_bytes());
    push_field(&mut out, b"MIT-MAGIC-COOKIE-1");
    push_field(&mut out, cookie);
    out
}

fn hostname() -> Result<String> {
    for path in ["/proc/sys/kernel/hostname", "/etc/hostname"] {
        if let Ok(text) = fs::read_to_string(path) {
            let name = text.trim();
            if !name.is_empty() {
                return Ok(name.to_string());
            }
        }
    }
    Err("could not determine the host name for the X authority file".into())
}

fn random_cookie() -> Result<[u8; 16]> {
    use std::io::Read;
    let mut cookie = [0u8; 16];
    fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut cookie))
        .map_err(|e| format!("could not read /dev/urandom: {e}"))?;
    Ok(cookie)
}

fn auth_path(display: u32) -> PathBuf {
    let dir = std::env::var_os("XDG_RUNTIME_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp"));
    dir.join(format!("spotify-headless-{display}.xauth"))
}

/// Create the file readable only by this user.
fn write_private(path: &Path, data: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let _ = fs::remove_file(path); // a stale file from a crashed run
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)?.write_all(data)
}

/// Start Xvfb and wait until its socket exists.
pub fn start(cfg: &Config) -> Result<XServer> {
    let xvfb = proc::which("Xvfb").ok_or("Xvfb not found (install the 'xvfb' package)")?;
    let display = pick_display(cfg.display_start, display_taken)
        .ok_or_else(|| format!("no free X display from :{} on", cfg.display_start))?;
    let auth = auth_path(display);
    write_private(&auth, &xauth_entry(&hostname()?, display, &random_cookie()?))
        .map_err(|e| format!("could not write {}: {e}", auth.display()))?;

    let log = proc::open_log(&cfg.log_dir, "xvfb")?;
    let log_err = log.try_clone()?;
    let mut cmd = Command::new(xvfb);
    cmd.arg(format!(":{display}"))
        .args(["-screen", "0", &cfg.resolution])
        .args(["-nolisten", "tcp", "-noreset", "-auth"])
        .arg(&auth)
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(log_err);
    let child = match proc::spawn_group(&mut cmd) {
        Ok(child) => child,
        Err(e) => {
            let _ = fs::remove_file(&auth);
            return Err(format!("could not start Xvfb: {e}").into());
        }
    };
    let mut server = XServer { display, auth_file: auth, proc: Managed::new("Xvfb", child) };

    let deadline = Instant::now() + Duration::from_secs(10);
    while !socket_path(display).exists() {
        if let Some(status) = server.exited() {
            return Err(format!("Xvfb exited ({status}); see {}", cfg.log_dir.join("xvfb.log").display()).into());
        }
        if Instant::now() > deadline {
            return Err("Xvfb did not come up within 10 seconds".into());
        }
        thread::sleep(Duration::from_millis(50));
    }
    Ok(server)
}

/// x11vnc on the loopback only, so a first-time login can be done over an SSH tunnel.
pub fn start_vnc(cfg: &Config, server: &XServer, port: u16) -> Result<Managed> {
    let x11vnc = proc::which("x11vnc").ok_or("x11vnc not found (install the 'x11vnc' package)")?;
    let log = proc::open_log(&cfg.log_dir, "x11vnc")?;
    let log_err = log.try_clone()?;
    let mut cmd = Command::new(x11vnc);
    cmd.arg("-display")
        .arg(server.display_str())
        .arg("-auth")
        .arg(&server.auth_file)
        .args(["-localhost", "-nopw", "-forever", "-shared", "-quiet", "-rfbport"])
        .arg(port.to_string())
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(log_err);
    let child = proc::spawn_group(&mut cmd).map_err(|e| format!("could not start x11vnc: {e}"))?;
    Ok(Managed::new("x11vnc", child))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn picks_first_free_display() {
        assert_eq!(pick_display(99, |n| n == 99 || n == 100), Some(101));
        assert_eq!(pick_display(5, |_| false), Some(5));
        assert_eq!(pick_display(5, |_| true), None);
    }

    #[test]
    fn xauth_entry_layout() {
        let cookie = [0xAB; 16];
        let entry = xauth_entry("host", 99, &cookie);
        let mut expected = vec![0x01, 0x00]; // FamilyLocal = 256
        expected.extend_from_slice(&[0, 4]);
        expected.extend_from_slice(b"host");
        expected.extend_from_slice(&[0, 2]);
        expected.extend_from_slice(b"99");
        expected.extend_from_slice(&[0, 18]);
        expected.extend_from_slice(b"MIT-MAGIC-COOKIE-1");
        expected.extend_from_slice(&[0, 16]);
        expected.extend_from_slice(&cookie);
        assert_eq!(entry, expected);
    }
}
