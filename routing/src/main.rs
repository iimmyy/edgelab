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
    let token = headers.get("authorization").and_then(|v| v.to_str().ok());
    if token != Some(format!("Bearer {}", identity.token).as_str()) {
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
struct Cursor {
    after: Option<u64>,
}
async fn view(
    Extract(server): Extract<Arc<Server>>,
    Query(cursor): Query<Cursor>,
) -> Result<Json<Arc<State>>, Failure> {
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
    Ok(Json(latest))
}
async fn health(Extract(server): Extract<Arc<Server>>) -> Json<Value> {
    let state = server.active.borrow();
    Json(
        json!({"ready":true,"generation":state.generation,"owners":state.owners.len(),
                "subscriber_slots":server.subscribers.available_permits()}),
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
    let state = match storage::load(&options.state, VIEW_LIMIT) {
        Ok(state) => state,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => State::default(),
        Err(e) => return Err(e.into()),
    };
    let (active, _) = watch::channel(Arc::new(state));
    let server = Arc::new(Server {
        config,
        path: options.state,
        active,
        writer: Arc::new(Mutex::new(())),
        subscribers: Semaphore::new(32),
    });
    let app = Router::new()
        .route("/snapshot", post(publish))
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
