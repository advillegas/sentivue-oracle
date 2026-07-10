// Embed the whole platform (tracked files only, via git archive) into the exe.
// oracle-desk is then a single self-sufficient artifact: installer + platform.
use std::process::Command;

fn main() {
    let out = std::env::var("OUT_DIR").unwrap();
    let dest = format!("{out}/payload.zip");
    let ok = Command::new("git")
        .args(["archive", "--format=zip", "--prefix=sentivue-oracle/", "-o", &dest, "HEAD"])
        .current_dir("..")
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if !ok {
        // Building outside a git checkout: ship without a payload (launcher-only).
        std::fs::write(&dest, b"").unwrap();
    }
    println!("cargo:rerun-if-changed=../.git/HEAD");
    println!("cargo:rerun-if-changed=build.rs");
}
