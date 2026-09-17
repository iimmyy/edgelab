package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"
)

type routeRecord struct {
	ID       string `json:"id"`
	App      string `json:"app"`
	Endpoint string `json:"endpoint"`
	Revision uint64 `json:"revision"`
	Deleted  bool   `json:"deleted"`
}
type routeSnapshot struct {
	Schema      int                    `json:"schema"`
	Owner       string                 `json:"owner"`
	Incarnation uint64                 `json:"incarnation"`
	Revision    uint64                 `json:"revision"`
	Records     map[string]routeRecord `json:"records"`
}
type workload struct {
	Unit     string `json:"unit"`
	App      string `json:"app"`
	Endpoint string `json:"endpoint"`
	Version  string `json:"version"`
	Kind     string `json:"kind"`
}
type daemonConfig struct {
	RouterAdminToken string              `json:"router_admin_token"`
	Workloads        map[string]workload `json:"workloads"`
	Owner            string              `json:"owner"`
	Applications     []string            `json:"applications"`
	Token            string              `json:"token"`
	AdminToken       string              `json:"admin_token"`
	Routers          []string            `json:"routers"`
	RestoreGuard     string              `json:"restore_guard"`
}
type registration struct {
	Operation string `json:"operation"`
	ID        string `json:"id"`
	App       string `json:"app"`
	Endpoint  string `json:"endpoint"`
	Deleted   bool   `json:"deleted"`
}
type instanceObservation struct {
	State     string `json:"state"`
	App       string `json:"app,omitempty"`
	Instance  string `json:"instance,omitempty"`
	Version   string `json:"version,omitempty"`
	Endpoint  string `json:"endpoint,omitempty"`
	CheckedMS int64  `json:"checked_ms"`
}
type workerDaemon struct {
	config          daemonConfig
	db              *sql.DB
	mu              sync.Mutex
	publications    map[string]any
	identities      map[string]instanceObservation
	statusVersion   int
	deploymentEpoch *int64
}

var routeName = regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,128}$`)

func serveWorker(args []string) error {
	flags := flag.NewFlagSet("serve", flag.ContinueOnError)
	configPath := flags.String("config", "", "owner configuration")
	statePath := flags.String("state", "", "intent database")
	address := flags.String("listen", "127.0.0.1:18301", "management address")
	statusVersion := flags.Int("status-version", 1, "status schema version: 2 requires deployment_epoch")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *statePath == "" || *configPath == "" {
		return errors.New("config and state required")
	}
	data, err := os.ReadFile(*configPath)
	if err != nil {
		return err
	}
	if len(data) > 65536 {
		return errors.New("config too large")
	}
	var config daemonConfig
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err = decoder.Decode(&config); err != nil {
		return err
	}
	if !routeName.MatchString(config.Owner) || len(config.Token) < 32 || len(config.AdminToken) < 32 || len(config.Routers) != 2 || !filepath.IsAbs(config.RestoreGuard) {
		return errors.New("invalid owner credentials, routers or external restore guard")
	}
	allowed := map[string]bool{}
	for _, app := range config.Applications {
		if !routeName.MatchString(app) {
			return errors.New("invalid application")
		}
		allowed[app] = true
	}
	if len(allowed) == 0 || len(allowed) > 64 {
		return errors.New("expected 1..64 applications")
	}
	for _, router := range config.Routers {
		if !strings.HasPrefix(router, "http://") {
			return errors.New("lab routers require explicit http URL")
		}
	}
	lock, err := os.OpenFile(*statePath+".lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return err
	}
	db, err := sql.Open("sqlite3", *statePath+"?_journal_mode=WAL&_synchronous=FULL&_busy_timeout=2000")
	if err != nil {
		return err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	var mode string
	var syncMode int
	if err = db.QueryRow("PRAGMA journal_mode").Scan(&mode); err != nil {
		return err
	}
	if err = db.QueryRow("PRAGMA synchronous").Scan(&syncMode); err != nil {
		return err
	}
	if mode != "wal" || syncMode != 2 {
		return errors.New("SQLite durability mismatch")
	}
	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS routing_state(id INTEGER PRIMARY KEY CHECK(id=1), body BLOB NOT NULL);
 CREATE TABLE IF NOT EXISTS routing_operations(id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, revision INTEGER NOT NULL);`)
	if err != nil {
		return err
	}
	initial, _ := json.Marshal(routeSnapshot{Schema: 1, Owner: config.Owner, Incarnation: 1, Revision: 1, Records: map[string]routeRecord{}})
	if _, err = db.Exec("INSERT OR IGNORE INTO routing_state(id,body) VALUES(1,?)", initial); err != nil {
		return err
	}
	daemon := workerDaemon{config: config, db: db, publications: map[string]any{}, identities: map[string]instanceObservation{}, statusVersion: *statusVersion}
	if *statusVersion == 2 {
		var epoch int64
		if err = db.QueryRow("SELECT deployment_epoch FROM routing_state WHERE id=1").Scan(&epoch); err != nil {
			return fmt.Errorf("status schema 2 readiness: %w", err)
		}
		daemon.deploymentEpoch = &epoch
	} else if *statusVersion != 1 {
		return errors.New("unsupported status schema")
	}
	current, err := daemon.snapshot()
	if err != nil {
		return err
	}
	if current.Owner != config.Owner {
		return errors.New("database belongs to another owner")
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/snapshot", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			http.Error(w, "method", 405)
			return
		}
		if !authorized(r, config.Token) {
			http.Error(w, "credential", 401)
			return
		}
		if _, err := os.Stat(config.RestoreGuard); err == nil || !os.IsNotExist(err) {
			http.Error(w, "publication disabled", 503)
			return
		}
		snapshot, err := daemon.snapshot()
		if err != nil {
			http.Error(w, err.Error(), 503)
			return
		}
		json.NewEncoder(w).Encode(snapshot)
	})
	mux.HandleFunc("/instances", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			http.Error(w, "method", 405)
			return
		}
		if !authorized(r, config.AdminToken) {
			http.Error(w, "credential", 401)
			return
		}
		if _, err := os.Stat(config.RestoreGuard); err == nil || !os.IsNotExist(err) {
			http.Error(w, "restore mode", 503)
			return
		}
		var request registration
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&request); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		if err := decoder.Decode(new(any)); err != io.EOF {
			http.Error(w, "trailing input", 400)
			return
		}
		if !routeName.MatchString(request.Operation) || !routeName.MatchString(request.ID) || !allowed[request.App] {
			http.Error(w, "invalid operation or application", 400)
			return
		}
		address, err := netip.ParseAddrPort(request.Endpoint)
		if err != nil || address.Port() == 0 || address.Addr().IsUnspecified() || address.Addr().IsMulticast() {
			http.Error(w, "expected literal backend endpoint", 400)
			return
		}
		request.Endpoint = address.String()
		expected, ok := config.Workloads[request.ID]
		if !ok || expected.App != request.App || expected.Endpoint != request.Endpoint {
			http.Error(w, "instance outside configured ownership", 400)
			return
		}
		revision, err := daemon.register(request)
		if err != nil {
			http.Error(w, err.Error(), 409)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"revision": revision, "durable": true})
		json.NewEncoder(os.Stdout).Encode(map[string]any{"event": "intent_committed", "owner": config.Owner, "operation": request.Operation, "instance": request.ID, "revision": revision, "deleted": request.Deleted})
	})
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		daemon.mu.Lock()
		observations := map[string]instanceObservation{}
		for id, observation := range daemon.identities {
			if observation.State == "verified" && time.Now().UnixMilli()-observation.CheckedMS > 5000 {
				observation.State = "stale"
			}
			observations[id] = observation
		}
		publications := map[string]any{}
		for router, result := range daemon.publications {
			publications[router] = result
		}
		daemon.mu.Unlock()
		json.NewEncoder(w).Encode(map[string]any{"owner": config.Owner, "publications": publications, "instances": observations, "status_version": daemon.statusVersion, "deployment_epoch": daemon.deploymentEpoch})
	})
	go daemon.publishLoop()
	server := http.Server{Addr: *address, Handler: mux, ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 4 * time.Second, IdleTimeout: 2 * time.Second, MaxHeaderBytes: 8192}
	listener, err := net.Listen("tcp", *address)
	if err != nil {
		return err
	}
	return server.Serve(&workerListener{Listener: listener, slots: make(chan struct{}, 32)})
}
func authorized(r *http.Request, token string) bool {
	return subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+token)) == 1
}
func (d *workerDaemon) snapshot() (routeSnapshot, error) {
	var data []byte
	var snapshot routeSnapshot
	if err := d.db.QueryRow("SELECT body FROM routing_state WHERE id=1").Scan(&data); err != nil {
		return snapshot, err
	}
	err := json.Unmarshal(data, &snapshot)
	return snapshot, err
}
func (d *workerDaemon) register(request registration) (uint64, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	tx, err := d.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	bytes, _ := json.Marshal(request)
	sum := sha256.Sum256(bytes)
	hash := hex.EncodeToString(sum[:])
	var oldHash string
	var revision uint64
	err = tx.QueryRow("SELECT request_hash,revision FROM routing_operations WHERE id=?", request.Operation).Scan(&oldHash, &revision)
	if err == nil {
		if oldHash != hash {
			return 0, errors.New("operation has different intended result")
		}
		return revision, nil
	}
	if err != sql.ErrNoRows {
		return 0, err
	}
	var count int
	if err = tx.QueryRow("SELECT count(*) FROM routing_operations").Scan(&count); err != nil {
		return 0, err
	}

	var data []byte
	if err = tx.QueryRow("SELECT body FROM routing_state WHERE id=1").Scan(&data); err != nil {
		return 0, err
	}
	var state routeSnapshot
	if err = json.Unmarshal(data, &state); err != nil {
		return 0, err
	}
	old, exists := state.Records[request.ID]
	if count >= 16384 && !(exists && !old.Deleted && request.Deleted) {
		return 0, errors.New("operation capacity reached; deletion reserve retained")
	}
	if exists && (old.App != request.App || old.Endpoint != request.Endpoint || old.Deleted && !request.Deleted) {
		return 0, errors.New("record conflicts with permanent identity")
	}
	if !exists && (request.Deleted || len(state.Records) >= 4096) {
		return 0, errors.New("unknown deletion or lifetime capacity")
	}
	for id, record := range state.Records {
		if record.Endpoint == request.Endpoint && id != request.ID {
			return 0, errors.New("endpoint permanently reserved")
		}
	}
	if state.Revision >= 1<<63-1 {
		return 0, errors.New("revision exhausted")
	}
	state.Revision++
	state.Records[request.ID] = routeRecord{ID: request.ID, App: request.App, Endpoint: request.Endpoint, Revision: state.Revision, Deleted: request.Deleted}
	encoded, err := json.Marshal(state)
	if err != nil {
		return 0, err
	}
	if _, err = tx.Exec("UPDATE routing_state SET body=? WHERE id=1", encoded); err != nil {
		return 0, err
	}
	if _, err = tx.Exec("INSERT INTO routing_operations(id,request_hash,revision) VALUES(?,?,?)", request.Operation, hash, state.Revision); err != nil {
		return 0, err
	}
	return state.Revision, tx.Commit()
}
func (d *workerDaemon) publishLoop() {
	client := http.Client{Timeout: 2 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	for {
		for _, router := range d.config.Routers {
			err := func() error {
				if _, err := os.Stat(d.config.RestoreGuard); err == nil || !os.IsNotExist(err) {
					return errors.New("publication disabled by external restore guard")
				}
				snapshot, err := d.snapshot()
				if err != nil {
					return err
				}
				if err = d.reconcileWorkloads(snapshot); err != nil {
					return err
				}
				data, err := json.Marshal(snapshot)
				if err != nil {
					return err
				}
				request, err := http.NewRequestWithContext(context.Background(), "POST", strings.TrimRight(router, "/")+"/snapshot", bytes.NewReader(data))
				if err != nil {
					return err
				}
				request.Header.Set("Content-Type", "application/json")
				request.Header.Set("Authorization", "Bearer "+d.config.Token)
				response, err := client.Do(request)
				if err != nil {
					return err
				}
				defer response.Body.Close()
				io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
				if response.StatusCode != 200 {
					return fmt.Errorf("routing publication HTTP %d", response.StatusCode)
				}
				return nil
			}()
			d.mu.Lock()
			if err != nil {
				d.publications[router] = map[string]any{"ok": false, "error": err.Error()}
			} else {
				d.publications[router] = map[string]any{"ok": true, "time_ms": time.Now().UnixMilli()}
			}
			d.mu.Unlock()
			if err != nil {
				log.Printf("publication unavailable: %v", err)
			}
		}
		time.Sleep(2 * time.Second)
	}
}

func (d *workerDaemon) reconcileWorkloads(snapshot routeSnapshot) error {
	for id, record := range snapshot.Records {
		if !record.Deleted {
			d.mu.Lock()
			d.identities[id] = instanceObservation{State: "unknown", CheckedMS: time.Now().UnixMilli()}
			d.mu.Unlock()
		}
		expected, ok := d.config.Workloads[id]
		if !ok || expected.App != record.App || expected.Endpoint != record.Endpoint ||
			!strings.HasPrefix(expected.Unit, "edgelab-") || !strings.HasSuffix(expected.Unit, ".service") ||
			strings.ContainsAny(expected.Unit, "/ \t\n") {
			return errors.New("workload requires ownership reconciliation")
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		active := exec.CommandContext(ctx, "systemctl", "is-active", "--quiet", expected.Unit).Run() == nil
		cancel()
		if record.Deleted && active || !record.Deleted && !active {
			action := "start"
			if record.Deleted {
				action = "stop"
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			output, err := exec.CommandContext(ctx, "systemctl", action, expected.Unit).CombinedOutput()
			cancel()
			if err != nil {
				return fmt.Errorf("supervisor %s: %w: %.256s", action, err, output)
			}
		}
		if record.Deleted {
			d.mu.Lock()
			delete(d.identities, id)
			d.mu.Unlock()
			continue
		}
		if expected.Kind == "objects" {
			client := http.Client{Timeout: time.Second}
			response, err := client.Get("http://" + expected.Endpoint + "/health")
			if err != nil {
				return err
			}
			var identity map[string]string
			err = json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&identity)
			response.Body.Close()
			if err != nil || response.StatusCode != 200 || identity["owner"] != id {
				return errors.New("object ownership not ready")
			}
		} else if expected.Kind == "echo" {
			connection, err := net.DialTimeout("tcp", expected.Endpoint, time.Second)
			if err != nil {
				return err
			}
			connection.SetDeadline(time.Now().Add(time.Second))
			var identity map[string]string
			err = json.NewDecoder(io.LimitReader(connection, 4096)).Decode(&identity)
			connection.Close()
			if err != nil || identity["app"] != record.App || identity["instance"] != id || identity["worker"] != snapshot.Owner || identity["version"] != expected.Version {
				return errors.New("workload serving identity not ready")
			}
		} else {
			return errors.New("unsupported workload identity protocol")
		}
		d.mu.Lock()
		version := expected.Version
		if expected.Kind == "objects" {
			version = ""
		}
		d.identities[id] = instanceObservation{State: "verified", App: record.App, Instance: id, Version: version, Endpoint: record.Endpoint, CheckedMS: time.Now().UnixMilli()}
		d.mu.Unlock()
	}
	return nil
}

type workerListener struct {
	net.Listener
	slots chan struct{}
}
type workerConnection struct {
	net.Conn
	release func()
	once    sync.Once
}

func (c *workerConnection) Close() error { err := c.Conn.Close(); c.once.Do(c.release); return err }
func (l *workerListener) Accept() (net.Conn, error) {
	for {
		connection, err := l.Listener.Accept()
		if err != nil {
			return nil, err
		}
		select {
		case l.slots <- struct{}{}:
			return &workerConnection{Conn: connection, release: func() { <-l.slots }}, nil
		default:
			connection.Close()
		}
	}
}
