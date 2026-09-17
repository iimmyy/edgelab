use crate::State;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

pub fn load(path: &Path, limit: usize) -> io::Result<State> {
    let mut bytes = Vec::new();
    File::open(path)?
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "state exceeds capacity",
        ));
    }
    let mut state: State = serde_json::from_slice(&bytes)?;
    state.after_restart();
    Ok(state)
}

// A single writer owns this directory. Never activate the candidate on an error.
pub fn save(path: &Path, state: &State) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("state requires parent directory"))?;
    let temporary = path.with_extension("pending");
    let bytes = serde_json::to_vec(state)?;
    let mut file = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    fs::rename(&temporary, path)?;
    File::open(parent)?.sync_all()
}
