//! The embedded platform: oracle-desk carries the entire repo payload inside
//! itself (git archive at build time) and self-extracts on first run — the exe
//! requires nothing but itself.

use std::io::Read;
use std::path::Path;

pub const PAYLOAD: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/payload.zip"));

pub fn has_payload() -> bool {
    PAYLOAD.len() > 100
}

/// Extract the embedded platform into `dest` (strips the archive prefix).
/// Returns the number of files written.
pub fn extract_to(dest: &Path) -> Result<usize, String> {
    if !has_payload() {
        return Err("this build carries no embedded payload".into());
    }
    let cursor = std::io::Cursor::new(PAYLOAD);
    let mut zip = zip::ZipArchive::new(cursor).map_err(|e| e.to_string())?;
    let mut written = 0usize;
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i).map_err(|e| e.to_string())?;
        let name = entry.name().to_string();
        let rel = name.strip_prefix("sentivue-oracle/").unwrap_or(&name);
        if rel.is_empty() {
            continue;
        }
        let path = dest.join(rel);
        if entry.is_dir() {
            std::fs::create_dir_all(&path).map_err(|e| e.to_string())?;
            continue;
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mut buf = Vec::with_capacity(entry.size() as usize);
        entry.read_to_end(&mut buf).map_err(|e| e.to_string())?;
        std::fs::write(&path, &buf).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        if let Some(mode) = entry.unix_mode() {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode));
        }
        written += 1;
    }
    // Keep a copy of this exe inside the platform so shortcuts/scripts find it.
    if let Ok(me) = std::env::current_exe() {
        let target = dest.join("desk/target/release").join(if cfg!(windows) {
            "oracle-desk.exe"
        } else {
            "oracle-desk"
        });
        if let Some(p) = target.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        if me != target {
            let _ = std::fs::copy(&me, &target);
        }
    }
    Ok(written)
}
