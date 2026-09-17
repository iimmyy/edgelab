use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::net::SocketAddr;
pub mod storage;

pub const RECORD_LIMIT: usize = 4096;
pub const VIEW_LIMIT: usize = 16 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub id: String,
    pub app: String,
    pub endpoint: SocketAddr,
    pub revision: u64,
    pub deleted: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub schema: u32,
    pub owner: String,
    pub incarnation: u64,
    pub revision: u64,
    pub records: BTreeMap<String, Record>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Recovery {
    pub operation: String,
    pub digest: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Owner {
    pub snapshot: Snapshot,
    pub reconciled_ms: u64,
    pub stale: bool,
    pub frozen_by: Option<String>,
    #[serde(default)]
    pub credential_hash: Option<String>,
    #[serde(default)]
    pub recovery: Option<Recovery>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct State {
    pub generation: u64,
    pub owners: BTreeMap<String, Owner>,
}

pub struct Policy {
    pub applications: BTreeSet<String>,
    pub record_limit: usize,
    pub view_limit: usize,
}

impl Policy {
    pub fn new(applications: BTreeSet<String>) -> Self {
        Self {
            applications,
            record_limit: RECORD_LIMIT,
            view_limit: VIEW_LIMIT,
        }
    }
}

fn name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
}

impl State {
    pub fn freeze(&self, owner: &str, operation: &str) -> Result<Self, String> {
        if !name(operation) {
            return Err("invalid recovery operation".into());
        }
        let mut candidate = self.clone();
        let entry = candidate.owners.get_mut(owner).ok_or("unknown owner")?;
        if entry
            .recovery
            .as_ref()
            .is_some_and(|r| r.operation == operation)
        {
            return Ok(candidate);
        }
        match &entry.frozen_by {
            Some(existing) if existing != operation => {
                return Err("another recovery owns the fence".into());
            }
            Some(_) => return Ok(candidate),
            None => entry.frozen_by = Some(operation.into()),
        }
        entry.stale = true;
        candidate.generation = candidate
            .generation
            .checked_add(1)
            .ok_or("generation exhausted")?;
        Ok(candidate)
    }

    pub fn recover(
        &self,
        operation: &str,
        next: Snapshot,
        credential_hash: String,
        policy: &Policy,
    ) -> Result<Self, String> {
        use sha2::{Digest, Sha256};
        if !name(operation)
            || credential_hash.len() != 64
            || !credential_hash.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err("invalid recovery identity".into());
        }
        let digest = format!(
            "{:x}",
            Sha256::digest(
                serde_json::to_vec(&(operation, &next, &credential_hash))
                    .map_err(|e| e.to_string())?
            )
        );
        let previous = self.owners.get(&next.owner).ok_or("unknown owner")?;
        if let Some(recovery) = &previous.recovery {
            if recovery.operation == operation {
                return if recovery.digest == digest {
                    Ok(self.clone())
                } else {
                    Err("recovery result conflict".into())
                };
            }
        }
        if previous.frozen_by.as_deref() != Some(operation)
            || next.incarnation <= previous.snapshot.incarnation
        {
            return Err("recovery requires matching fence and newer incarnation".into());
        }
        let mut base = self.clone();
        let owner = base.owners.get_mut(&next.owner).unwrap();
        owner.snapshot.incarnation = next.incarnation;
        owner.frozen_by = None;
        let id = next.owner.clone();
        let mut candidate = base.accept(&id, next, policy, previous.reconciled_ms)?;
        let owner = candidate.owners.get_mut(&id).unwrap();
        owner.stale = true;
        owner.credential_hash = Some(credential_hash);
        owner.recovery = Some(Recovery {
            operation: operation.into(),
            digest,
        });
        Ok(candidate)
    }

    pub fn after_restart(&mut self) {
        for owner in self.owners.values_mut() {
            owner.stale = true;
        }
    }

    // Return a candidate; persistence must succeed before the caller activates it.
    pub fn accept(
        &self,
        authenticated_owner: &str,
        next: Snapshot,
        policy: &Policy,
        now_ms: u64,
    ) -> Result<Self, String> {
        if next.schema != 1
            || next.owner != authenticated_owner
            || !name(&next.owner)
            || next.incarnation == 0
            || next.revision == 0
        {
            return Err("invalid schema or owner authority".into());
        }
        if next.records.len() > policy.record_limit {
            return Err("lifetime record capacity exceeded".into());
        }
        let previous = self.owners.get(&next.owner);
        if let Some(previous) = previous {
            if previous.frozen_by.is_some() {
                return Err("owner frozen for recovery".into());
            }
            if next.incarnation != previous.snapshot.incarnation {
                return Err("incarnation changes require recovery".into());
            }
            if next.revision < previous.snapshot.revision {
                return Err("snapshot revision regressed".into());
            }
            if next.revision == previous.snapshot.revision && next != previous.snapshot {
                return Err("conflicting snapshot revision".into());
            }
            for (id, old) in &previous.snapshot.records {
                let new = next.records.get(id).ok_or("accepted history omitted")?;
                if new.id != old.id
                    || new.app != old.app
                    || new.endpoint != old.endpoint
                    || new.revision < old.revision
                    || (old.deleted && !new.deleted)
                    || (new.revision == old.revision && new != old)
                {
                    return Err("record conflicts with accepted history".into());
                }
            }
        }
        let reservations: BTreeMap<_, _> = self
            .owners
            .iter()
            .flat_map(|(owner_id, owner)| {
                owner
                    .snapshot
                    .records
                    .values()
                    .map(move |record| (record.endpoint, (owner_id, &record.id)))
            })
            .collect();
        let mut endpoints = BTreeSet::new();
        for (id, record) in &next.records {
            if id != &record.id
                || !name(id)
                || !name(&record.app)
                || !policy.applications.contains(&record.app)
                || record.revision == 0
                || record.revision > next.revision
                || record.endpoint.port() == 0
                || record.endpoint.ip().is_unspecified()
                || record.endpoint.ip().is_multicast()
            {
                return Err("invalid record or application ownership".into());
            }
            if !endpoints.insert(record.endpoint) {
                return Err("endpoint reserved by another instance".into());
            }
            if let Some((owner_id, id)) = reservations.get(&record.endpoint) {
                if *owner_id != &next.owner || *id != &record.id {
                    return Err("endpoint reserved by another instance".into());
                }
            }
        }
        let mut candidate = self.clone();
        candidate.generation = self
            .generation
            .checked_add(1)
            .ok_or("generation exhausted")?;
        candidate.owners.insert(
            next.owner.clone(),
            Owner {
                snapshot: next,
                reconciled_ms: now_ms,
                stale: false,
                frozen_by: None,
                credential_hash: previous.and_then(|o| o.credential_hash.clone()),
                recovery: previous.and_then(|o| o.recovery.clone()),
            },
        );
        // Budget the largest deletion/revision representation at admission. This
        // leaves deletion possible even when an owner has reached its capacity.
        let mut reserved = candidate.clone();
        reserved.generation = u64::MAX;
        for owner in reserved.owners.values_mut() {
            owner.reconciled_ms = u64::MAX;
            owner.snapshot.revision = u64::MAX;
            owner.snapshot.incarnation = u64::MAX;
            owner.stale = false;
            owner.frozen_by = Some("x".repeat(128));
            owner.credential_hash = Some("x".repeat(64));
            owner.recovery = Some(Recovery {
                operation: "x".repeat(128),
                digest: "x".repeat(64),
            });
            for record in owner.snapshot.records.values_mut() {
                record.revision = u64::MAX;
                record.deleted = false;
            }
        }
        if serde_json::to_vec(&reserved)
            .map_err(|e| e.to_string())?
            .len()
            > policy.view_limit
        {
            return Err("complete view capacity exceeded".into());
        }
        Ok(candidate)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot() -> Snapshot {
        let record = Record {
            id: "one".into(),
            app: "echo".into(),
            endpoint: "127.0.0.1:9000".parse().unwrap(),
            revision: 1,
            deleted: false,
        };
        Snapshot {
            schema: 1,
            owner: "worker-1".into(),
            incarnation: 1,
            revision: 1,
            records: [(record.id.clone(), record)].into(),
        }
    }
    fn policy() -> Policy {
        Policy::new(["echo".into()].into())
    }

    #[test]
    fn tombstones_survive_replay_and_reserve_endpoints() {
        let original = snapshot();
        let state = State::default()
            .accept("worker-1", original.clone(), &policy(), 1)
            .unwrap();
        let mut deletion = original.clone();
        deletion.revision = 2;
        deletion.records.get_mut("one").unwrap().revision = 2;
        deletion.records.get_mut("one").unwrap().deleted = true;
        let state = state
            .accept("worker-1", deletion.clone(), &policy(), 2)
            .unwrap();
        assert!(state.accept("worker-1", original, &policy(), 3).is_err());
        let mut reuse = snapshot();
        reuse.owner = "worker-2".into();
        assert!(state.accept("worker-2", reuse, &policy(), 3).is_err());
        deletion.incarnation = 2;
        assert!(state.accept("worker-1", deletion, &policy(), 3).is_err());
    }

    #[test]
    fn reject_omission_conflict_and_impersonation_without_mutation() {
        let original = snapshot();
        let state = State::default()
            .accept("worker-1", original.clone(), &policy(), 1)
            .unwrap();
        let before = serde_json::to_vec(&state).unwrap();
        let mut omitted = original.clone();
        omitted.revision = 2;
        omitted.records.clear();
        assert!(state.accept("worker-1", omitted, &policy(), 2).is_err());
        let mut conflict = original.clone();
        conflict.records.get_mut("one").unwrap().deleted = true;
        assert!(state.accept("worker-1", conflict, &policy(), 2).is_err());
        assert!(state.accept("worker-2", original, &policy(), 2).is_err());
        assert_eq!(before, serde_json::to_vec(&state).unwrap());
    }

    #[test]
    fn capacity_still_allows_deletion_and_restart_preserves_source_time() {
        let mut policy = policy();
        policy.record_limit = 1;
        let original = snapshot();
        let mut state = State::default()
            .accept("worker-1", original.clone(), &policy, 40)
            .unwrap();
        state.after_restart();
        assert!(state.owners["worker-1"].stale);
        assert_eq!(state.owners["worker-1"].reconciled_ms, 40);
        let mut deletion = original;
        deletion.revision = 2;
        deletion.records.get_mut("one").unwrap().revision = 2;
        deletion.records.get_mut("one").unwrap().deleted = true;
        let state = state
            .accept("worker-1", deletion.clone(), &policy, 50)
            .unwrap();
        let mut extra = deletion.records["one"].clone();
        extra.id = "two".into();
        extra.endpoint.set_port(9001);
        deletion.records.insert(extra.id.clone(), extra);
        deletion.revision = 3;
        assert!(state.accept("worker-1", deletion, &policy, 60).is_err());
    }
}
