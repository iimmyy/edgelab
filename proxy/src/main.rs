use clap::Parser;
use hickory_resolver::{Resolver, TokioResolver};
use serde::Deserialize;
use std::{
    collections::{HashMap, HashSet},
    io,
    net::{IpAddr, SocketAddr},
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::copy_bidirectional_with_sizes,
    net::{TcpListener, TcpStream},
    signal::unix::{SignalKind, signal},
    sync::{OwnedSemaphorePermit, Semaphore},
    task::JoinSet,
    time::{Instant, timeout, timeout_at},
};
use tracing::{error, info, warn};

#[derive(Parser, Clone)]
struct Options {
    #[arg(long)]
    config: PathBuf,
    #[arg(long, default_value = "127.0.0.1")]
    listen_ip: IpAddr,
    #[arg(long, default_value_t = 128, value_parser = clap::value_parser!(u32).range(1..=4096))]
    limit: u32,
    #[arg(long, default_value_t = 8192, value_parser = clap::value_parser!(u32).range(1..=65536))]
    buffer_bytes: u32,
    #[arg(long, default_value_t = 500, value_parser = clap::value_parser!(u32).range(1..=60000))]
    target_ms: u32,
    #[arg(long, default_value_t = 2000, value_parser = clap::value_parser!(u32).range(1..=60000))]
    connect_ms: u32,
    #[arg(long, default_value_t = 5000, value_parser = clap::value_parser!(u32).range(1..=60000))]
    drain_ms: u32,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase", deny_unknown_fields)]
struct Config {
    apps: Vec<AppConfig>,
}
#[derive(Deserialize)]
#[serde(rename_all = "PascalCase", deny_unknown_fields)]
struct AppConfig {
    name: String,
    ports: Vec<u16>,
    targets: Vec<String>,
}

struct Admission {
    permits: Arc<Semaphore>,
    next: AtomicUsize,
}
struct App {
    name: String,
    targets: Vec<String>,
    admission: Arc<Admission>,
}
struct View {
    ports: HashMap<u16, Arc<App>>,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn parse(bytes: &[u8]) -> io::Result<Config> {
    if bytes.len() > 1024 * 1024 {
        return Err(invalid("configuration exceeds 1 MiB"));
    }
    let config: Config = serde_json::from_slice(bytes)?;
    if config.apps.is_empty() || config.apps.len() > 64 {
        return Err(invalid("expected 1..64 applications"));
    }
    let mut names = HashSet::new();
    let mut ports = HashSet::new();
    for app in &config.apps {
        if app.name.is_empty() || app.name.len() > 128 || !names.insert(&app.name) {
            return Err(invalid("invalid or duplicate application name"));
        }
        if app.ports.is_empty() || app.targets.is_empty() || app.targets.len() > 64 {
            return Err(invalid("expected listeners and 1..64 targets"));
        }
        for port in &app.ports {
            if *port == 0 || !ports.insert(*port) || ports.len() > 256 {
                return Err(invalid("invalid, duplicate or excessive listeners"));
            }
        }
        for target in &app.targets {
            let Some((host, port)) = target.rsplit_once(':') else {
                return Err(invalid("target requires host:port"));
            };
            if host.is_empty()
                || target.len() > 512
                || target.chars().any(char::is_whitespace)
                || port.parse::<u16>().unwrap_or(0) == 0
            {
                return Err(invalid("invalid target"));
            }
        }
    }
    Ok(config)
}

async fn prepare(
    options: &Options,
    admissions: &mut HashMap<String, std::sync::Weak<Admission>>,
    listeners: &HashMap<u16, Arc<TcpListener>>,
) -> io::Result<(View, HashMap<u16, Arc<TcpListener>>)> {
    // A bounded read also protects against a file growing between metadata and read.
    use tokio::io::AsyncReadExt;
    if !tokio::fs::metadata(&options.config).await?.is_file() {
        return Err(invalid("configuration must be a regular file"));
    }
    let file = tokio::fs::File::open(&options.config).await?;
    let mut bytes = Vec::new();
    file.take(1024 * 1024 + 1).read_to_end(&mut bytes).await?;
    let config = parse(&bytes)?;
    let mut ports = HashMap::new();
    let mut bound = HashMap::new();
    admissions.retain(|_, a| a.strong_count() > 0);
    for item in config.apps {
        let admission = admissions
            .get(&item.name)
            .and_then(|a| a.upgrade())
            .unwrap_or_else(|| {
                Arc::new(Admission {
                    permits: Arc::new(Semaphore::new(options.limit as usize)),
                    next: AtomicUsize::new(0),
                })
            });
        admissions.insert(item.name.clone(), Arc::downgrade(&admission));
        let app = Arc::new(App {
            name: item.name,
            targets: item.targets,
            admission,
        });
        for port in item.ports {
            let listener = match listeners.get(&port) {
                Some(listener) => listener.clone(),
                None => Arc::new(TcpListener::bind((options.listen_ip, port)).await?),
            };
            bound.insert(port, listener);
            ports.insert(port, app.clone());
        }
    }
    Ok((View { ports }, bound))
}

type Accepted = (u16, io::Result<TcpStream>);
fn accept_one(tasks: &mut JoinSet<Accepted>, port: u16, listener: Arc<TcpListener>) {
    tasks.spawn(async move {
        let result = listener.accept().await.map(|(socket, _)| socket);
        if result.is_err() {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        (port, result)
    });
}

async fn connect_target(target: &str, resolver: &TokioResolver) -> io::Result<TcpStream> {
    if let Ok(address) = target.parse::<SocketAddr>() {
        return TcpStream::connect(address).await;
    }
    let (host, port) = target
        .rsplit_once(':')
        .ok_or_else(|| invalid("target requires port"))?;
    let port: u16 = port.parse().map_err(|_| invalid("invalid target port"))?;
    let addresses = resolver.lookup_ip(host).await.map_err(io::Error::other)?;
    let mut failure = io::Error::new(io::ErrorKind::NotFound, "no DNS addresses");
    for ip in addresses.iter().take(16) {
        match TcpStream::connect((ip, port)).await {
            Ok(stream) => return Ok(stream),
            Err(err) => failure = err,
        }
    }
    Err(failure)
}

async fn connect(app: &App, options: &Options, resolver: &TokioResolver) -> io::Result<TcpStream> {
    let start = app.admission.next.fetch_add(1, Ordering::Relaxed) % app.targets.len();
    let deadline = Instant::now() + Duration::from_millis(options.connect_ms.into());
    for offset in 0..app.targets.len() {
        let target = &app.targets[(start + offset) % app.targets.len()];
        let until = deadline.min(Instant::now() + Duration::from_millis(options.target_ms.into()));
        match timeout_at(until, connect_target(target, resolver)).await {
            Ok(Ok(stream)) => {
                info!(event="connected", app=%app.name, target);
                return Ok(stream);
            }
            Ok(Err(err)) => warn!(event="connect_failed", app=%app.name, target, error=%err),
            Err(_) => warn!(event="connect_timeout", app=%app.name, target),
        }
        if Instant::now() >= deadline {
            break;
        }
    }
    Err(io::Error::new(
        io::ErrorKind::ConnectionRefused,
        "backend establishment exhausted",
    ))
}

async fn forward(
    mut client: TcpStream,
    app: Arc<App>,
    options: Options,
    _permit: OwnedSemaphorePermit,
    _global_permit: OwnedSemaphorePermit,
    resolver: TokioResolver,
) {
    let started = Instant::now();
    let result = async {
        let mut backend = connect(&app, &options, &resolver).await?;
        client.set_nodelay(true)?;
        backend.set_nodelay(true)?;
        copy_bidirectional_with_sizes(
            &mut client,
            &mut backend,
            options.buffer_bytes as usize,
            options.buffer_bytes as usize,
        )
        .await
    }
    .await;
    match result {
        Ok((up, down)) => {
            info!(event="closed", app=%app.name, up, down, elapsed_ms=started.elapsed().as_millis() as u64)
        }
        Err(err) => {
            warn!(event="session_failed", app=%app.name, error=%err, elapsed_ms=started.elapsed().as_millis() as u64)
        }
    }
}

#[tokio::main]
async fn main() -> io::Result<()> {
    tracing_subscriber::fmt()
        .json()
        .with_writer(std::io::stderr)
        .init();
    let options = Options::parse();
    let mut builder = Resolver::builder_tokio().map_err(io::Error::other)?;
    builder.options_mut().cache_size = 256;
    builder.options_mut().max_active_requests = 32;
    let resolver = builder.build().map_err(io::Error::other)?;
    let global = Arc::new(Semaphore::new(1024));
    let mut admissions = HashMap::new();
    let (mut view, mut listeners) = prepare(&options, &mut admissions, &HashMap::new()).await?;
    let mut accepts = JoinSet::new();
    let mut sessions = JoinSet::new();
    for (&port, listener) in &listeners {
        accept_one(&mut accepts, port, listener.clone());
    }
    let mut term = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;
    let mut reload = signal(SignalKind::hangup())?;
    let mut tick = tokio::time::interval(Duration::from_secs(1));
    info!(
        event = "ready",
        listeners = listeners.len(),
        limit = options.limit,
        buffer_bytes = options.buffer_bytes,
        target_ms = options.target_ms,
        connect_ms = options.connect_ms
    );
    loop {
        tokio::select! {
            _ = term.recv() => break,
            _ = interrupt.recv() => break,
            _ = reload.recv() => {
                match timeout(Duration::from_secs(2), prepare(&options, &mut admissions, &listeners)).await.unwrap_or_else(|_| Err(io::Error::new(io::ErrorKind::TimedOut, "configuration load timeout"))) {
                    Ok((candidate, bound)) => {
                        accepts.abort_all();
                        while accepts.join_next().await.is_some() {}
                        view = candidate;
                        listeners = bound;
                        for (&port, listener) in &listeners { accept_one(&mut accepts, port, listener.clone()); }
                        info!(event="reloaded", listeners=listeners.len());
                    }
                    Err(err) => warn!(event="reload_rejected", error=%err),
                }
            }
            Some(result) = accepts.join_next() => {
                let (port, accepted) = result.map_err(io::Error::other)?;
                accept_one(&mut accepts, port, listeners[&port].clone());
                match accepted {
                    Ok(socket) => {
                        while let Some(result) = sessions.try_join_next() {
                            if let Err(err) = result { error!(event="session_task_failed", error=%err); }
                        }
                        let app = view.ports[&port].clone();
                        match app.admission.permits.clone().try_acquire_owned() {
                            Ok(permit) => {
                                match global.clone().try_acquire_owned() {
                                    Ok(global_permit) => { sessions.spawn(forward(socket, app, options.clone(), permit, global_permit, resolver.clone())); }
                                    Err(_) => warn!(event="global_overload", app=%app.name),
                                }
                            }
                            Err(_) => warn!(event="overload", app=%app.name, port),
                        }
                    }
                    Err(err) => warn!(event="accept_failed", port, error=%err),
                }
            }
            Some(result) = sessions.join_next() => { if let Err(err) = result { error!(event="session_task_failed", error=%err); } }
            _ = tick.tick() => {
                for (name, admission) in &admissions {
                    if let Some(a) = admission.upgrade() { info!(event="admission", app=%name, active=options.limit as usize-a.permits.available_permits()); }
                }
                info!(event="tasks", sessions=sessions.len(), listeners=listeners.len());
            }
        }
    }
    accepts.abort_all();
    while accepts.join_next().await.is_some() {}
    drop(listeners);
    info!(
        event = "draining",
        sessions = sessions.len(),
        grace_ms = options.drain_ms
    );
    if timeout(Duration::from_millis(options.drain_ms.into()), async {
        while sessions.join_next().await.is_some() {}
    })
    .await
    .is_err()
    {
        warn!(event = "drain_expired", sessions = sessions.len());
        sessions.abort_all();
        while sessions.join_next().await.is_some() {}
    }
    info!(event = "stopped");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_ambiguous_configuration() {
        for data in [
            r#"{"Apps":[]}"#,
            r#"{"Apps":[{"Name":"a","Ports":[1,1],"Targets":["localhost:3"]}]}"#,
            r#"{"Apps":[{"Name":"a","Ports":[1],"Targets":["localhost:0"]}]}"#,
        ] {
            assert!(parse(data.as_bytes()).is_err());
        }
    }
}
