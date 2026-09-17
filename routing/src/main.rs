mod listener;
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Query, State as Extract},
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use clap::Parser;
use edgelab_routing::{Policy, Snapshot, State, VIEW_LIMIT, storage};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::PathBuf,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{Mutex, Semaphore, watch};

#[derive(Parser)]
struct Options {
    #[arg(long)]
    config: PathBuf,
    #[arg(long)]
    state: PathBuf,
    #[arg(long, default_value = "127.0.0.1:18201")]
    listen: std::net::SocketAddr,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    #[serde(default)]
    admin_token: String,
    owners: BTreeMap<String, Identity>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    token: String,
    applications: BTreeSet<String>,
}
struct Server {
    config: Config,
    path: PathBuf,
    active: watch::Sender<Arc<State>>,
    writer: Arc<Mutex<()>>,
    subscribers: Semaphore,
    coalesced: std::sync::atomic::AtomicU64,
}
type Failure = (StatusCode, Json<Value>);
fn failure(status: StatusCode, message: impl ToString) -> Failure {
    (status, Json(json!({"error":message.to_string()})))
}
fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

async fn publish(
    Extract(server): Extract<Arc<Server>>,
    headers: HeaderMap,
    Json(snapshot): Json<Snapshot>,
) -> Result<Json<Value>, Failure> {
    let identity = server
        .config
        .owners
        .get(&snapshot.owner)
        .ok_or_else(|| failure(StatusCode::UNAUTHORIZED, "unknown owner"))?;
    use sha2::{Digest, Sha256};
    let token = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("");
    let expected = server
        .active
        .borrow()
        .owners
        .get(&snapshot.owner)
        .and_then(|owner| owner.credential_hash.clone())
        .unwrap_or_else(|| format!("{:x}", Sha256::digest(identity.token.as_bytes())));
    if format!("{:x}", Sha256::digest(token.as_bytes())) != expected {
        return Err(failure(
            StatusCode::UNAUTHORIZED,
            "invalid owner credential",
        ));
    }
    let writer = server.writer.clone().try_lock_owned().map_err(|_| {
        failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "writer busy; retry complete snapshot",
        )
    })?;
    let current_hash = server
        .active
        .borrow()
        .owners
        .get(&snapshot.owner)
        .and_then(|owner| owner.credential_hash.clone())
        .unwrap_or_else(|| format!("{:x}", Sha256::digest(identity.token.as_bytes())));
    if current_hash != expected {
        return Err(failure(
            StatusCode::UNAUTHORIZED,
            "publishing credential fenced",
        ));
    }
    let policy = Policy::new(identity.applications.clone());
    let current = server.active.borrow().clone();
    let candidate = current
        .accept(&snapshot.owner.clone(), snapshot, &policy, now_ms())
        .map_err(|e| failure(StatusCode::CONFLICT, e))?;
    let candidate = Arc::new(candidate);
    let path = server.path.clone();
    tokio::spawn(async move {
        let _writer = writer;
        let persisted = candidate.clone();
        tokio::task::spawn_blocking(move || storage::save(&path, &persisted))
            .await
            .map_err(|e| failure(StatusCode::INTERNAL_SERVER_ERROR, e))?
            .map_err(|e| failure(StatusCode::INSUFFICIENT_STORAGE, e))?;
        let generation = candidate.generation;
        server.active.send_replace(candidate);
        Ok(Json(json!({"accepted":true,"generation":generation})))
    })
    .await
    .map_err(|e| failure(StatusCode::INTERNAL_SERVER_ERROR, e))?
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RecoveryRequest {
    owner: String,
    operation: String,
    snapshot: Option<Snapshot>,
    credential_hash: Option<String>,
}
async fn recover(
    Extract(server): Extract<Arc<Server>>,
    headers: HeaderMap,
    Json(request): Json<RecoveryRequest>,
) -> Result<Json<Value>, Failure> {
    if server.config.admin_token.len() < 32
        || headers.get("authorization").and_then(|v| v.to_str().ok())
            != Some(format!("Bearer {}", server.config.admin_token).as_str())
    {
        return Err(failure(
            StatusCode::UNAUTHORIZED,
            "administrative credential required",
        ));
    }
    let identity = server
        .config
        .owners
        .get(&request.owner)
        .ok_or_else(|| failure(StatusCode::NOT_FOUND, "owner"))?;
    let writer = server
        .writer
        .clone()
        .try_lock_owned()
        .map_err(|_| failure(StatusCode::SERVICE_UNAVAILABLE, "writer busy"))?;
    let current = server.active.borrow().clone();
    let result = match (request.snapshot, request.credential_hash) {
        (None, None) => current.freeze(&request.owner, &request.operation),
        (Some(snapshot), Some(hash)) if snapshot.owner == request.owner => current.recover(
            &request.operation,
            snapshot,
            hash,
            &Policy::new(identity.applications.clone()),
        ),
        _ => Err("incomplete recovery result".into()),
    };
    let candidate = Arc::new(result.map_err(|e| failure(StatusCode::CONFLICT, e))?);
    tokio::spawn(async move {
        let _writer = writer;
        let persisted = candidate.clone();
        let path = server.path.clone();
        tokio::task::spawn_blocking(move || storage::save(&path, &persisted))
            .await
            .map_err(|e| failure(StatusCode::INTERNAL_SERVER_ERROR, e))?
            .map_err(|e| failure(StatusCode::INSUFFICIENT_STORAGE, e))?;
        let response = serde_json::to_value(&candidate.owners[&request.owner])
            .map_err(|e| failure(StatusCode::INTERNAL_SERVER_ERROR, e))?;
        server.active.send_replace(candidate);
        Ok(Json(response))
    })
    .await
    .map_err(|e| failure(StatusCode::INTERNAL_SERVER_ERROR, e))?
}

#[derive(Deserialize)]
struct Cursor {
    after: Option<u64>,
}
async fn view(
    Extract(server): Extract<Arc<Server>>,
    Query(cursor): Query<Cursor>,
) -> Result<(HeaderMap, Json<Arc<State>>), Failure> {
    let _slot = server.subscribers.try_acquire().map_err(|_| {
        failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "subscriber capacity reached",
        )
    })?;
    let mut receiver = server.active.subscribe();
    let generation = receiver.borrow_and_update().generation;
    if cursor.after == Some(generation) {
        let _ = tokio::time::timeout(Duration::from_secs(2), receiver.changed()).await;
    }
    // watch retains one complete replacement, never an update backlog.
    let latest = receiver.borrow_and_update().clone();
    let skipped = cursor
        .after
        .map(|after| latest.generation.saturating_sub(after).saturating_sub(1))
        .unwrap_or(0);
    server
        .coalesced
        .fetch_add(skipped, std::sync::atomic::Ordering::Relaxed);
    let mut headers = HeaderMap::new();
    headers.insert(
        "x-skipped-generations",
        skipped.to_string().parse().unwrap(),
    );
    Ok((headers, Json(latest)))
}
async fn health(Extract(server): Extract<Arc<Server>>) -> Json<Value> {
    let state = server.active.borrow();
    Json(
        json!({"ready":true,"generation":state.generation,"owners":state.owners.len(),
                "subscriber_slots":server.subscribers.available_permits(),"pending_view_capacity":1,"coalesced_deliveries":server.coalesced.load(std::sync::atomic::Ordering::Relaxed)}),
    )
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let options = Options::parse();
    let bytes = std::fs::read(&options.config)?;
    if bytes.len() > 65536 {
        return Err("owner configuration too large".into());
    }
    let config: Config = serde_json::from_slice(&bytes)?;
    if config.owners.is_empty()
        || config.owners.len() > 16
        || config.owners.values().any(|owner| {
            owner.token.len() < 32 || owner.token.len() > 128 || owner.applications.is_empty()
        })
    {
        return Err("expected 1..16 owners with credentials and applications".into());
    }
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(options.state.with_extension("lock"))?;
    fs2::FileExt::try_lock_exclusive(&lock)?;
    let state = match storage::load(&options.state, VIEW_LIMIT) {
        Ok(state) => state,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => State::default(),
        Err(e) => return Err(e.into()),
    };
    let mut validated = State::default();
    for (id, owner) in &state.owners {
        let identity = config
            .owners
            .get(id)
            .ok_or("persisted owner missing from configuration")?;
        validated = validated.accept(
            id,
            owner.snapshot.clone(),
            &Policy::new(identity.applications.clone()),
            owner.reconciled_ms,
        )?;
    }
    let (active, _) = watch::channel(Arc::new(state));
    let server = Arc::new(Server {
        config,
        path: options.state,
        active,
        writer: Arc::new(Mutex::new(())),
        subscribers: Semaphore::new(32),
        coalesced: std::sync::atomic::AtomicU64::new(0),
    });
    let app = Router::new()
        .route("/snapshot", post(publish))
        .route("/recovery", post(recover))
        .route("/view", get(view))
        .route("/health", get(health))
        .layer(DefaultBodyLimit::max(VIEW_LIMIT))
        .with_state(server);
    let listener = tokio::net::TcpListener::bind(options.listen).await?;
    axum::serve(
        listener::BoundedListener {
            socket: listener,
            slots: Arc::new(Semaphore::new(64)),
        },
        app,
    )
    .await?;
    Ok(())
}
