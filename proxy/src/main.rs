mod dynamic;
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
    net::{TcpListener, TcpSocket, TcpStream},
    signal::unix::{SignalKind, signal},
    sync::{OwnedSemaphorePermit, Semaphore},
    task::JoinSet,
    time::{Instant, timeout, timeout_at},
};
use tracing::{error, info, warn};

#[derive(Parser, Clone)]
struct Options {
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long, conflicts_with = "config")]
    dynamic: Option<PathBuf>,
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
    /// Deliberate validation delay for lab fault tests; normal reloads use zero.
    #[arg(long, default_value_t = 0, value_parser = clap::value_parser!(u32).range(0..=1000))]
    reload_delay_ms: u32,
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
#[derive(Clone)]
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
    delayed: bool,
) -> io::Result<(View, HashMap<u16, Arc<TcpListener>>)> {
    // A bounded read also protects against a file growing between metadata and read.
    use tokio::io::AsyncReadExt;
    let config_path = options
        .config
        .as_ref()
        .ok_or_else(|| invalid("--config or --dynamic required"))?;
    if !tokio::fs::metadata(config_path).await?.is_file() {
        return Err(invalid("configuration must be a regular file"));
    }
    let file = tokio::fs::File::open(config_path).await?;
    let mut bytes = Vec::new();
    file.take(1024 * 1024 + 1).read_to_end(&mut bytes).await?;
    if delayed {
        info!(event = "reload_candidate_read");
        tokio::time::sleep(Duration::from_millis(options.reload_delay_ms.into())).await;
    }
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

async fn connect_address(
    address: SocketAddr,
    deadline: Instant,
    app: &str,
    target: &str,
) -> io::Result<TcpStream> {
    let socket = if address.is_ipv4() {
        TcpSocket::new_v4()?
    } else {
        TcpSocket::new_v6()?
    };
    let observer = socket2::SockRef::from(&socket).try_clone()?;
    match timeout_at(deadline, socket.connect(address)).await {
        Ok(Ok(stream)) => Ok(stream),
        Ok(Err(err)) => {
            warn!(event="connect_failed", stage="tcp_connect", app, target, remote=%address,
                  local=?observer.local_addr().ok().and_then(|a| a.as_socket()),
                  os_error=err.raw_os_error(), error=%err);
            Err(err)
        }
        Err(_) => {
            let pending = observer.take_error();
            warn!(event="connect_timeout", stage="tcp_connect", deadline_source="application", app, target,
                  remote=%address, local=?observer.local_addr().ok().and_then(|a| a.as_socket()),
                  so_error=pending.as_ref().ok().map(|e| e.as_ref().and_then(io::Error::raw_os_error).unwrap_or(0)),
                  diagnostic_error=?pending.err());
            Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "backend establishment deadline",
            ))
        }
    }
}

async fn connect_target(
    target: &str,
    resolver: &TokioResolver,
    deadline: Instant,
    app: &str,
) -> io::Result<TcpStream> {
    if let Ok(address) = target.parse::<SocketAddr>() {
        return connect_address(address, deadline, app, target).await;
    }
    let (host, port) = target
        .rsplit_once(':')
        .ok_or_else(|| invalid("target requires port"))?;
    let port: u16 = port.parse().map_err(|_| invalid("invalid target port"))?;
    let addresses = match timeout_at(deadline, resolver.lookup_ip(host)).await {
        Ok(Ok(addresses)) => addresses,
        Ok(Err(err)) => {
            warn!(event="connect_failed", stage="dns", app, target, error=%err);
            return Err(io::Error::other(err));
        }
        Err(_) => {
            warn!(
                event = "connect_timeout",
                stage = "dns",
                deadline_source = "application",
                app,
                target
            );
            return Err(io::Error::new(io::ErrorKind::TimedOut, "DNS deadline"));
        }
    };
    let mut failure = io::Error::new(io::ErrorKind::NotFound, "no DNS addresses");
    for ip in addresses.iter().take(16) {
        match connect_address(SocketAddr::new(ip, port), deadline, app, target).await {
            Ok(stream) => return Ok(stream),
            Err(err) => failure = err,
        }
        if Instant::now() >= deadline {
            break;
        }
    }
    Err(failure)
}

async fn connect(app: &App, options: &Options, resolver: &TokioResolver) -> io::Result<TcpStream> {
    let start = app.admission.next.fetch_add(1, Ordering::Relaxed) % app.targets.len();
    let deadline = Instant::now() + Duration::from_millis(options.connect_ms.into());
    let mut failure = io::Error::new(io::ErrorKind::NotFound, "no backend");
    for offset in 0..app.targets.len() {
        let target = &app.targets[(start + offset) % app.targets.len()];
        let until = deadline.min(Instant::now() + Duration::from_millis(options.target_ms.into()));
        match connect_target(target, resolver, until, &app.name).await {
            Ok(stream) => {
                info!(event="connected", app=%app.name, target);
                return Ok(stream);
            }
            Err(err) => failure = err,
        }
        if Instant::now() >= deadline {
            break;
        }
    }
    Err(failure)
}

struct Prepared {
    view: View,
    listeners: HashMap<u16, Arc<TcpListener>>,
    admissions: HashMap<String, std::sync::Weak<Admission>>,
}

fn validate_reload(
    tasks: &mut JoinSet<(u64, io::Result<Prepared>)>,
    generation: u64,
    options: Options,
    mut admissions: HashMap<String, std::sync::Weak<Admission>>,
    listeners: HashMap<u16, Arc<TcpListener>>,
) {
    tasks.spawn(async move {
        let result = timeout(
            Duration::from_secs(2),
            prepare(&options, &mut admissions, &listeners, true),
        )
        .await
        .unwrap_or_else(|_| {
            Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "configuration load timeout",
            ))
        })
        .map(|(view, listeners)| Prepared {
            view,
            listeners,
            admissions,
        });
        (generation, result)
    });
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
    let name = app.name.clone();
    let result = async move {
        let mut backend = connect(&app, &options, &resolver).await?;
        drop(app);
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
            info!(event="closed", app=%name, up, down, elapsed_ms=started.elapsed().as_millis() as u64)
        }
        Err(err) => {
            warn!(event="session_failed", app=%name, error=%err, elapsed_ms=started.elapsed().as_millis() as u64)
        }
    }
}

fn main() -> io::Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(run());
    runtime.shutdown_background();
    result
}

async fn run() -> io::Result<()> {
    let (writer, guard) = tracing_appender::non_blocking::NonBlockingBuilder::default()
        .buffered_lines_limit(128)
        .lossy(true)
        .finish(std::io::stderr());
    let log_errors = writer.error_counter();
    tracing_subscriber::fmt().json().with_writer(writer).init();
    let options = Options::parse();
    let mut builder = Resolver::builder_tokio().map_err(io::Error::other)?;
    builder.options_mut().cache_size = 256;
    builder.options_mut().max_active_requests = 32;
    let resolver = builder.build().map_err(io::Error::other)?;
    let global = Arc::new(Semaphore::new(1024));
    let mut admissions = HashMap::new();
    let mut dynamic = if let Some(path) = &options.dynamic {
        Some(dynamic::start(path, &options, &mut admissions).await?)
    } else {
        None
    };
    let (mut view, mut listeners) = if let Some(control) = &mut dynamic {
        (
            control.views.borrow_and_update().as_ref().clone(),
            control.listeners.clone(),
        )
    } else {
        prepare(&options, &mut admissions, &HashMap::new(), false).await?
    };
    let mut accepts = JoinSet::new();
    let mut sessions = JoinSet::new();
    let mut validations = JoinSet::new();
    let mut generation = 0_u64;
    let mut pending = None;
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
            biased;
            _ = term.recv() => break,
            _ = interrupt.recv() => break,
            changed = async {
                match &mut dynamic {
                    Some(control) => control.views.changed().await,
                    None => std::future::pending().await,
                }
            } => {
                changed.map_err(io::Error::other)?;
                view = dynamic.as_mut().unwrap().views.borrow_and_update().as_ref().clone();
                info!(event="dynamic_view_activated");
            }
            _ = reload.recv() => {
                if dynamic.is_some() { warn!(event="static_reload_disabled"); continue; }
                generation += 1;
                if validations.is_empty() {
                    validate_reload(&mut validations, generation, options.clone(), admissions.clone(), listeners.clone());
                } else {
                    pending = Some(generation);
                }
                info!(event="reload_requested", generation);
            }
            Some(completed) = validations.join_next() => {
                let (finished, result) = completed.map_err(io::Error::other)?;
                if finished != generation {
                    info!(event="reload_superseded", generation=finished);
                    drop(result);
                } else {
                    match result {
                        Ok(candidate) => {
                            accepts.abort_all();
                            while accepts.join_next().await.is_some() {}
                            view = candidate.view;
                            listeners = candidate.listeners;
                            admissions = candidate.admissions;
                            for (&port, listener) in &listeners { accept_one(&mut accepts, port, listener.clone()); }
                            info!(event="reloaded", generation=finished, listeners=listeners.len());
                        }
                        Err(err) => warn!(event="reload_rejected", generation=finished, error=%err.to_string().chars().take(512).collect::<String>()),
                    }
                }
                if let Some(next) = pending.take() {
                    validate_reload(&mut validations, next, options.clone(), admissions.clone(), listeners.clone());
                }
            }
            _ = tick.tick() => {
                for (name, admission) in &admissions {
                    if let Some(a) = admission.upgrade() { info!(event="admission", app=%name, active=options.limit as usize-a.permits.available_permits()); }
                }
                info!(event="tasks", sessions=sessions.len(), listeners=listeners.len(), validations=validations.len(), pending_reload=pending.is_some(), logs_dropped=log_errors.dropped_lines());
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
                        if app.targets.is_empty() { warn!(event="route_unavailable", app=%app.name); continue; }
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

        }
    }
    if let Some(control) = &dynamic {
        control.stop();
    }
    let drain_deadline = Instant::now() + Duration::from_millis(options.drain_ms.into());
    validations.abort_all();
    while validations.join_next().await.is_some() {}
    accepts.abort_all();
    while accepts.join_next().await.is_some() {}
    drop(listeners);
    info!(
        event = "draining",
        sessions = sessions.len(),
        grace_ms = options.drain_ms
    );
    if timeout_at(drain_deadline, async {
        while sessions.join_next().await.is_some() {}
    })
    .await
    .is_err()
    {
        warn!(event = "drain_expired", sessions = sessions.len());
        sessions.abort_all();
        while sessions.join_next().await.is_some() {}
    }
    info!(event = "stopped", logs_dropped = log_errors.dropped_lines());
    // WorkerGuard flushes synchronously. Its thread may outlive our remaining grace.
    let (flushed, done) = tokio::sync::oneshot::channel();
    std::thread::spawn(move || {
        drop(guard);
        let _ = flushed.send(());
    });
    let _ = timeout_at(drain_deadline, done).await;
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
