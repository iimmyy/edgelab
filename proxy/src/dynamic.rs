use super::{Admission, App, Options, View, invalid};
use axum::{Json, Router, routing::get};
use edgelab_routing::{Policy, State, VIEW_LIMIT, storage};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    io,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::{Arc, Weak, atomic::AtomicUsize},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    net::TcpListener,
    sync::{RwLock, Semaphore, watch},
    task::JoinHandle,
};
use tracing::{info, warn};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    routers: Vec<String>,
    cache: PathBuf,
    management: SocketAddr,
    applications: BTreeMap<String, Vec<u16>>,
}
pub(super) struct Control {
    pub views: watch::Receiver<Arc<View>>,
    pub listeners: HashMap<u16, Arc<TcpListener>>,
    task: JoinHandle<()>,
}
impl Control {
    pub fn stop(&self) {
        self.task.abort();
    }
}
struct Status {
    current: Option<State>,
    persistence_error: Option<String>,
    delivery_ms: Option<u64>,
    routing_error: Option<String>,
}
fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
fn validate(state: &State, config: &Config, previous: Option<&State>) -> io::Result<()> {
    if state.owners.len() > 16 {
        return Err(invalid("too many owners"));
    }
    let policy = Policy::new(config.applications.keys().cloned().collect());
    let mut rebuilt = State::default();
    for (id, owner) in &state.owners {
        rebuilt = rebuilt
            .accept(id, owner.snapshot.clone(), &policy, owner.reconciled_ms)
            .map_err(|e| invalid(&e))?;
    }
    if let Some(previous) = previous {
        for (id, old) in &previous.owners {
            let next = state
                .owners
                .get(id)
                .ok_or_else(|| invalid("cached owner history omitted"))?;
            if next.snapshot.incarnation < old.snapshot.incarnation {
                return Err(invalid("owner incarnation regressed"));
            }
            for (key, record) in &old.snapshot.records {
                let new = next
                    .snapshot
                    .records
                    .get(key)
                    .ok_or_else(|| invalid("cached record omitted"))?;
                if new.endpoint != record.endpoint
                    || new.app != record.app
                    || new.revision < record.revision
                    || (record.deleted && !new.deleted)
                    || (new.revision == record.revision && new != record)
                {
                    return Err(invalid("cached history regressed"));
                }
            }
        }
    }
    Ok(())
}
fn make_view(
    config: &Config,
    state: Option<&State>,
    admissions: &HashMap<String, Arc<Admission>>,
) -> View {
    let mut ports = HashMap::new();
    for (name, listeners) in &config.applications {
        let mut targets = BTreeSet::new();
        if let Some(state) = state {
            for owner in state.owners.values() {
                for record in owner.snapshot.records.values() {
                    if &record.app == name && !record.deleted {
                        targets.insert(record.endpoint.to_string());
                    }
                }
            }
        }
        let app = Arc::new(App {
            name: name.clone(),
            targets: targets.into_iter().collect(),
            admission: admissions[name].clone(),
        });
        for port in listeners {
            ports.insert(*port, app.clone());
        }
    }
    View { ports }
}
async fn fetch(client: &reqwest::Client, url: &str) -> io::Result<State> {
    let mut response = client
        .get(format!("{}/view", url.trim_end_matches('/')))
        .send()
        .await
        .map_err(io::Error::other)?;
    if !response.status().is_success() {
        return Err(io::Error::other(format!(
            "routing HTTP {}",
            response.status()
        )));
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(io::Error::other)? {
        if bytes.len() + chunk.len() > VIEW_LIMIT {
            return Err(invalid("routing view too large"));
        }
        bytes.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}
pub(super) async fn start(
    path: &Path,
    options: &Options,
    weak: &mut HashMap<String, Weak<Admission>>,
) -> io::Result<Control> {
    let bytes = tokio::fs::read(path).await?;
    if bytes.len() > 65536 {
        return Err(invalid("dynamic configuration too large"));
    }
    let config: Config = serde_json::from_slice(&bytes)?;
    if config.routers.is_empty()
        || config.routers.len() > 2
        || config.applications.is_empty()
        || config.applications.len() > 64
    {
        return Err(invalid("expected 1..2 routers and 1..64 applications"));
    }
    for url in &config.routers {
        let parsed = reqwest::Url::parse(url).map_err(io::Error::other)?;
        if parsed.scheme() != "http"
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || parsed.query().is_some()
            || parsed.fragment().is_some()
        {
            return Err(invalid("invalid lab router URL"));
        }
    }
    let mut listeners = HashMap::new();
    let mut admissions = HashMap::new();
    for (name, ports) in &config.applications {
        if name.is_empty() || name.len() > 128 || ports.is_empty() {
            return Err(invalid("invalid application listeners"));
        }
        let admission = Arc::new(Admission {
            permits: Arc::new(Semaphore::new(options.limit as usize)),
            next: AtomicUsize::new(0),
        });
        weak.insert(name.clone(), Arc::downgrade(&admission));
        admissions.insert(name.clone(), admission);
        for port in ports {
            if *port == 0 || listeners.contains_key(port) || listeners.len() >= 256 {
                return Err(invalid("duplicate or excessive listeners"));
            }
            listeners.insert(
                *port,
                Arc::new(TcpListener::bind((options.listen_ip, *port)).await?),
            );
        }
    }
    let loaded = storage::load(&config.cache, VIEW_LIMIT).and_then(|state| {
        validate(&state, &config, None)?;
        Ok(state)
    });
    let (current, routing_error) = match loaded {
        Ok(state) => (Some(state), None),
        Err(error) => {
            warn!(event="dynamic_cache_unavailable", error=%error);
            (None, Some(error.to_string()))
        }
    };
    let (sender, views) =
        watch::channel(Arc::new(make_view(&config, current.as_ref(), &admissions)));
    let status = Arc::new(RwLock::new(Status {
        current,
        persistence_error: None,
        delivery_ms: None,
        routing_error,
    }));
    let read_status = status.clone();
    let management = Router::new().route("/status", get(move || {
        let status = read_status.clone();
        async move {
            let status = status.read().await;
            let owners: BTreeMap<_, Value> = status.current.as_ref().map(|s| s.owners.iter().map(|(id, owner)| {
                (id.clone(), json!({"reconciled_ms":owner.reconciled_ms, "stale":owner.stale || now_ms().saturating_sub(owner.reconciled_ms)>5000}))
            }).collect()).unwrap_or_default();
            Json(json!({"mode":"dynamic","available":status.current.is_some(),"generation":status.current.as_ref().map(|s|s.generation),
                "owners":owners,"delivery_ms":status.delivery_ms,"persistence_error":status.persistence_error,"routing_error":status.routing_error}))
        }
    }));
    let admin = TcpListener::bind(config.management).await?;
    tokio::spawn(async move {
        if let Err(error) = axum::serve(admin, management).await {
            warn!(event="management_failed",error=%error);
        }
    });
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(io::Error::other)?;
    let task = tokio::spawn(async move {
        loop {
            let mut accepted = false;
            for router in &config.routers {
                let result = fetch(&client, router).await;
                let candidate = match result {
                    Ok(candidate) => candidate,
                    Err(error) => {
                        status.write().await.routing_error = Some(error.to_string());
                        continue;
                    }
                };
                {
                    let active = status.read().await;
                    if let Err(error) = validate(&candidate, &config, active.current.as_ref()) {
                        drop(active);
                        status.write().await.routing_error = Some(error.to_string());
                        continue;
                    }
                }
                let path = config.cache.clone();
                let persisted = candidate.clone();
                let result =
                    tokio::task::spawn_blocking(move || storage::save(&path, &persisted)).await;
                match result {
                    Ok(Ok(())) => {
                        let view = make_view(&config, Some(&candidate), &admissions);
                        let mut active = status.write().await;
                        active.current = Some(candidate);
                        active.delivery_ms = Some(now_ms());
                        active.persistence_error = None;
                        active.routing_error = None;
                        sender.send_replace(Arc::new(view));
                        accepted = true;
                        break;
                    }
                    error => {
                        status.write().await.persistence_error = Some(format!("{error:?}"));
                    }
                }
            }
            if !accepted {
                info!(event = "dynamic_update_unavailable");
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    });
    Ok(Control {
        views,
        listeners,
        task,
    })
}
