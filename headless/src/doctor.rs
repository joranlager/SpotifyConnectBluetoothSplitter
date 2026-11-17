//! `spotify-headless doctor`: report what is installed and whether audio is reachable.

use crate::audio;
use crate::cli::Config;
use crate::proc;

pub fn run(cfg: &Config) -> i32 {
    let mut missing = false;
    let checks: [(&str, &str, bool); 5] = [
        ("Xvfb (virtual display)", "Xvfb", true),
        ("Spotify client", cfg.spotify_bin.as_str(), true),
        ("pactl (sink control)", "pactl", true),
        ("parec (PCM capture)", "parec", cfg.pcm_out.is_some()),
        ("x11vnc (first login)", "x11vnc", cfg.vnc_port.is_some()),
    ];
    for (label, bin, required) in checks {
        match proc::which(bin) {
            Some(path) => println!("[ok]       {label}: {}", path.display()),
            None if required => {
                println!("[missing]  {label}: '{bin}' not found");
                missing = true;
            }
            None => println!("[optional] {label}: '{bin}' not found (only needed with the matching option)"),
        }
    }
    match audio::check_server() {
        Ok(name) => println!("[ok]       audio server: {name}"),
        Err(e) => {
            println!("[missing]  audio server: {e}");
            missing = true;
        }
    }
    if missing {
        1
    } else {
        0
    }
}
