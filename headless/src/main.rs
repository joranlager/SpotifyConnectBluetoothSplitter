//! spotify-headless: run the official Spotify desktop client headless on Linux.
//!
//! It wires together, and supervises:
//!   1. a virtual PulseAudio/PipeWire sink that receives all of Spotify's sound,
//!   2. an Xvfb virtual display (the client is a GUI app and needs one),
//!   3. the Spotify client itself, pinned to that display and that sink,
//!   4. optionally a PCM capture of the sink's monitor, written to stdout or a FIFO
//!      so another program (the speaker splitter) can read the decoded audio,
//!   5. optionally x11vnc, to sign in once.
//!
//! The Spotify client registers itself as a Spotify Connect device while it runs and
//! is logged in, so a phone can transfer playback to it like to any other device.

macro_rules! log_info {
    ($($t:tt)*) => { eprintln!("[spotify-headless] {}", format_args!($($t)*)) };
}
macro_rules! log_warn {
    ($($t:tt)*) => { eprintln!("[spotify-headless] warning: {}", format_args!($($t)*)) };
}

mod audio;
mod capture;
mod cli;
mod doctor;
mod proc;
mod session;
mod spotify;
mod xserver;

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

fn main() {
    std::process::exit(match real_main() {
        Ok(code) => code,
        Err(e) => {
            eprintln!("error: {e}");
            1
        }
    });
}

fn real_main() -> Result<i32> {
    let (action, cfg) = cli::parse(std::env::args().skip(1))?;
    match action {
        cli::Action::Help => {
            print!("{}", cli::USAGE);
            Ok(0)
        }
        cli::Action::Doctor => Ok(doctor::run(&cfg)),
        cli::Action::Run => {
            if !cfg!(target_os = "linux") {
                return Err("spotify-headless only runs on Linux".into());
            }
            let stop = Arc::new(AtomicBool::new(false));
            let flag = Arc::clone(&stop);
            ctrlc::set_handler(move || flag.store(true, Ordering::SeqCst))
                .map_err(|e| format!("could not install the signal handler: {e}"))?;
            session::run(&cfg, stop)?;
            Ok(0)
        }
    }
}
