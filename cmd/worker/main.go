package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	_ "github.com/mattn/go-sqlite3"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
)

var faultPoint = func(string) {}

func fatal(e error) {
	json.NewEncoder(os.Stdout).Encode(map[string]any{"ready": false, "error": e.Error()})
	os.Exit(1)
}
func main() {
	root := flag.String("root", "/var/lib/edgelab-worker", "owned worker root")
	storeURL := flag.String("store", "http://127.0.0.1:9200", "owned fixture store")
	id := flag.String("operation", "", "idempotency key")
	image := flag.String("manifest", "", "manifest sha256 digest")
	size := flag.Int64("manifest-bytes", 0, "pinned manifest length")
	crash := flag.String("crash-after", "", "lab process interruption point")
	unpack := flag.String("unpack-root", "", "internal extraction child")
	dataLimit := flag.Float64("data-limit", 80, "thinpool data percent ceiling")
	metadataLimit := flag.Float64("metadata-limit", 70, "thinpool metadata percent ceiling")
	flag.Parse()
	if os.Geteuid() != 0 || *root != "/var/lib/edgelab-worker" || !validDigest(*image) || *size < 1 || *size > maxBlob || !regexp.MustCompile(`^[a-zA-Z0-9_-]{1,64}$`).MatchString(*id) || *dataLimit <= 0 || *dataLimit > 80 || *metadataLimit <= 0 || *metadataLimit > 70 {
		fatal(errors.New("invalid worker arguments"))
	}
	info, e := os.Lstat(*root)
	if e != nil || !info.IsDir() || info.Mode().Perm() != 0700 || info.Sys().(*syscall.Stat_t).Uid != 0 {
		fatal(errors.New("untrusted worker directory"))
	}
	blob, e := os.ReadFile(filepath.Join(*root, "pool.json"))
	if e != nil {
		fatal(e)
	}
	var p pool
	if e = json.Unmarshal(blob, &p); e != nil {
		fatal(e)
	}
	machine, e := os.ReadFile("/etc/machine-id")
	if e != nil || strings.TrimSpace(string(machine)) != p.Machine {
		fatal(errors.New("worker ownership mismatch"))
	}
	backing, e := command("losetup", "-n", "-O", "BACK-FILE", p.Device)
	if e != nil || backing != filepath.Join(*root, "disk") {
		fatal(errors.New("worker device outside allowlist"))
	}
	storage := storage{*root, p, *dataLimit, *metadataLimit}
	if _, e = storage.inspect("pool"); e != nil {
		fatal(e)
	}
	time.AfterFunc(90*time.Second, func() { os.Exit(124) })
	cache := filepath.Join(*root, "blobs")
	if e = os.MkdirAll(cache, 0700); e != nil {
		fatal(e)
	}
	desc := descriptor{"application/vnd.oci.image.manifest.v1+json", *image, *size}
	if *unpack != "" {
		base := "image_" + strings.TrimPrefix(*image, "sha256:")[:24]
		expected := filepath.Join(*root, "mounts", base)
		if *unpack != expected {
			fatal(errors.New("invalid extraction root"))
		}
		if _, e = command("findmnt", "--mountpoint", expected); e != nil {
			fatal(e)
		}
		_, layers, e := loadLayers(*storeURL, cache, desc)
		if e != nil {
			fatal(e)
		}
		if e = syscall.Chroot(expected); e != nil {
			fatal(e)
		}
		if e = os.Chdir("/"); e != nil {
			fatal(e)
		}
		if e = applyLayers(layers); e != nil {
			fatal(e)
		}
		hash, e := treeDigest("/")
		if e != nil {
			fatal(e)
		}
		marker, _ := json.Marshal(map[string]string{"manifest": *image, "tree": hash})
		if e = durableFile("/.edgelab-image.json", marker); e != nil {
			fatal(e)
		}
		return
	}
	lock, e := os.OpenFile(filepath.Join(*root, "worker.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		fatal(e)
	}
	defer lock.Close()
	if e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		fatal(errors.New("worker busy"))
	}
	db, e := sql.Open("sqlite3", filepath.Join(*root, "intent.db")+"?_journal_mode=WAL&_synchronous=FULL&_busy_timeout=5000")
	if e != nil {
		fatal(e)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	var journal string
	var synchronous int
	if e = db.QueryRow("PRAGMA journal_mode").Scan(&journal); e != nil {
		fatal(e)
	}
	if e = db.QueryRow("PRAGMA synchronous").Scan(&synchronous); e != nil {
		fatal(e)
	}
	if journal != "wal" || synchronous != 2 {
		fatal(errors.New("SQLite durability mismatch"))
	}
	if _, e = db.Exec("CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, image TEXT NOT NULL, size INTEGER NOT NULL, phase TEXT NOT NULL, snapshot TEXT NOT NULL, uuid TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')"); e != nil {
		fatal(e)
	}
	base := "image_" + strings.TrimPrefix(*image, "sha256:")[:24]
	snapshot := "operation_" + digest([]byte(*id))[:24]
	var count int
	if e = db.QueryRow("SELECT count(*) FROM operations WHERE id != ?", *id).Scan(&count); e != nil {
		fatal(e)
	}
	if count >= 128 {
		fatal(errors.New("operation capacity reached"))
	}
	if _, e = db.Exec("INSERT OR IGNORE INTO operations(id,image,size,phase,snapshot) VALUES(?,?,?,'requested',?)", *id, *image, *size, snapshot); e != nil {
		fatal(e)
	}
	var oldImage, phase, oldSnapshot, oldUUID string
	var oldSize int64
	if e = db.QueryRow("SELECT image,size,phase,snapshot,uuid FROM operations WHERE id=?", *id).Scan(&oldImage, &oldSize, &phase, &oldSnapshot, &oldUUID); e != nil {
		fatal(e)
	}
	if oldImage != *image || oldSize != *size || oldSnapshot != snapshot {
		fatal(errors.New("operation conflict"))
	}
	faultPoint = func(point string) {
		if *crash == point {
			os.Exit(77)
		}
	}
	stage := func(next string) {
		if _, e := db.Exec("UPDATE operations SET phase=?,error='' WHERE id=?", next, *id); e != nil {
			fatal(e)
		}
		json.NewEncoder(os.Stdout).Encode(map[string]string{"operation": *id, "phase": next})
		faultPoint(next)
	}
	failed := func(err error) {
		_, _ = db.Exec("UPDATE operations SET error=? WHERE id=?", err.Error(), *id)
		fatal(err)
	}
	faultPoint("requested")
	if e = storage.capacity(0); e != nil {
		failed(e)
	}
	if _, _, e = loadLayers(*storeURL, cache, desc); e != nil {
		failed(e)
	}
	stage("verified")
	origin, e := storage.create(base, "image_"+strings.TrimPrefix(*image, "sha256:"), "")
	if e != nil {
		failed(e)
	}
	faultPoint("lv")
	mount, e := storage.mount(base, true)
	valid := false
	if e != nil {
		if kind, err := command("blkid", "-s", "TYPE", "-o", "value", "/dev/edgelab_worker/"+base); err == nil && kind == "ext4" {
			failed(e)
		}
	}
	if e == nil {
		b, err := os.ReadFile(filepath.Join(mount, ".edgelab-image.json"))
		if err == nil {
			var marker map[string]string
			if json.Unmarshal(b, &marker) == nil && marker["manifest"] == *image {
				hash, err := treeDigest(mount)
				valid = err == nil && hash == marker["tree"]
			}
		}
		if e = storage.unmount(base); e != nil {
			failed(e)
		}
	}
	if !valid {
		if strings.Contains(origin.Attributes, "r") {
			failed(errors.New("sealed image verification failed"))
		}
		if e = storage.capacity(maxExpanded); e != nil {
			failed(e)
		}
		if _, e = command("mkfs.ext4", "-q", "-F", "/dev/edgelab_worker/"+base); e != nil {
			failed(e)
		}
		faultPoint("format")
		mount, e = storage.mount(base, false)
		if e != nil {
			failed(e)
		}
		faultPoint("mount")
		executable, e := os.Executable()
		if e != nil {
			failed(e)
		}
		if _, e = command(executable, "--operation", *id, "--manifest", *image, "--manifest-bytes", fmt.Sprint(*size), "--store", *storeURL, "--unpack-root", mount); e != nil {
			failed(e)
		}
		faultPoint("unpack")
		if e = storage.unmount(base); e != nil {
			failed(e)
		}
		faultPoint("unmounted")
	}
	if _, e = command("lvchange", "--devices", p.Device, "-pr", "edgelab_worker/"+base); e != nil {
		failed(e)
	}
	faultPoint("seal")
	stage("sealed")
	snap, e := storage.create(snapshot, "operation_"+digest([]byte(*id)), base)
	if e != nil {
		failed(e)
	}
	faultPoint("snapshot")
	if oldUUID != "" && oldUUID != snap.UUID {
		failed(errors.New("snapshot UUID changed"))
	}
	if _, e = command("lvchange", "--devices", p.Device, "-prw", "edgelab_worker/"+snapshot); e != nil {
		failed(e)
	}
	faultPoint("writable")
	if _, e = db.Exec("UPDATE operations SET phase='registered',uuid=?,error='' WHERE id=?", snap.UUID, *id); e != nil {
		failed(e)
	}
	faultPoint("registered")
	json.NewEncoder(os.Stdout).Encode(map[string]any{"ready": true, "operation": *id, "image": *image, "origin": "/dev/edgelab_worker/" + base, "snapshot": "/dev/edgelab_worker/" + snapshot, "snapshot_uuid": snap.UUID, "sqlite_journal": journal, "sqlite_synchronous": synchronous})
}
