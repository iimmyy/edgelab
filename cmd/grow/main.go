package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	_ "github.com/mattn/go-sqlite3"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const mib = int64(1 << 20)

type fixture struct {
	Machine string   `json:"machine"`
	Devices []string `json:"devices"`
	LV      string   `json:"lv"`
	UUID    string   `json:"uuid"`
	Mount   string   `json:"mount"`
}

func run(name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	b, e := exec.CommandContext(ctx, name, args...).CombinedOutput()
	if e != nil {
		return "", fmt.Errorf("%s: %w: %s", name, e, b)
	}
	return strings.TrimSpace(string(b)), nil
}
func fail(err error) {
	json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": false, "error": err.Error()})
	os.Exit(1)
}
func number(s string) (int64, error) {
	n, e := strconv.ParseFloat(strings.TrimSpace(s), 64)
	return int64(n), e
}
func main() {
	root := flag.String("root", "/var/lib/edgelab-r2", "owned fixture root")
	op := flag.String("operation", "", "stable operation ID")
	target := flag.Int64("target-mib", 0, "absolute target, otherwise threshold based")
	threshold := flag.Int("threshold", 80, "filesystem usage percent")
	maximum := flag.Int64("max-mib", 6144, "allocation limit")
	reserve := flag.Int64("reserve-mib", 512, "VG reserve")
	crash := flag.String("crash-after", "", "intent, lv, filesystem, or complete")
	watch := flag.Bool("watch", false, "poll every two seconds")
	flag.Parse()
	if os.Geteuid() != 0 || *threshold < 1 || *threshold > 100 || *maximum < 1 || *reserve < 0 || *target < 0 {
		fail(errors.New("invalid privileges or bounds"))
	}
	if *watch && (*op != "" || *target != 0) {
		fail(errors.New("watch uses threshold and generated operation IDs"))
	}
	if *root != "/var/lib/edgelab-r2" || *maximum > 6144 || *target > 6144 {
		fail(errors.New("outside fixture allocation bounds"))
	}
	*target = (*target + 3) / 4 * 4
	info, err := os.Lstat(*root)
	if err != nil || !info.IsDir() || info.Mode().Perm() != 0700 || info.Sys().(*syscall.Stat_t).Uid != 0 {
		fail(errors.New("untrusted fixture directory"))
	}
	marker, err := os.ReadFile(filepath.Join(*root, "marker.json"))
	var marked struct {
		Machine string `json:"machine"`
	}
	if err != nil || json.Unmarshal(marker, &marked) != nil {
		fail(errors.New("unmarked environment"))
	}
	b, e := os.ReadFile(filepath.Join(*root, "storage.json"))
	if e != nil {
		fail(e)
	}
	var f fixture
	if e = json.Unmarshal(b, &f); e != nil {
		fail(e)
	}
	machine, e := os.ReadFile("/etc/machine-id")
	if e != nil || strings.TrimSpace(string(machine)) != f.Machine || len(f.Devices) != 4 || marked.Machine != f.Machine || f.LV != "/dev/edgelab_r2/data" || f.Mount != filepath.Join(*root, "volume") {
		fail(errors.New("fixture ownership mismatch"))
	}
	for i, d := range f.Devices {
		back, e := run("losetup", "-n", "-O", "BACK-FILE", d)
		if e != nil || back != filepath.Join(*root, fmt.Sprintf("disk%d", i)) {
			fail(errors.New("loop device outside allowlist"))
		}
	}
	devices := strings.Join(f.Devices, ",")
	lvm := func(cmd, field string) (string, error) {
		return run(cmd, "--devices", devices, "--noheadings", "--units", "b", "--nosuffix", "-o", field, f.LV)
	}
	uuid, e := lvm("lvs", "lv_uuid")
	if e != nil || uuid != f.UUID {
		fail(errors.New("LV identity mismatch"))
	}
	lock, e := os.OpenFile(filepath.Join(*root, "grow.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		fail(e)
	}
	defer lock.Close()
	if e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		fail(errors.New("controller busy"))
	}
	db, e := sql.Open("sqlite3", filepath.Join(*root, "intent.db")+"?_journal_mode=WAL&_synchronous=FULL&_busy_timeout=5000")
	if e != nil {
		fail(e)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	var journal string
	var sync int
	if e = db.QueryRow("PRAGMA journal_mode").Scan(&journal); e != nil {
		fail(e)
	}
	if e = db.QueryRow("PRAGMA synchronous").Scan(&sync); e != nil {
		fail(e)
	}
	if journal != "wal" || sync != 2 {
		fail(errors.New("SQLite durability settings mismatch"))
	}
	_, e = db.Exec("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, uuid TEXT NOT NULL, target INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
	if e != nil {
		fail(e)
	}
	stop := func(stage string) {
		if *crash == stage {
			os.Exit(77)
		}
	}
	step := func() error {
		mounted, err := run("findmnt", "--noheadings", "--mountpoint", f.Mount, "-o", "UUID")
		actualUUID, uuidErr := run("blkid", "-s", "UUID", "-o", "value", f.LV)
		if err != nil || uuidErr != nil || mounted == "" || mounted != actualUUID {
			return errors.New("owned filesystem is not mounted")
		}

		raw, e := lvm("lvs", "lv_size")
		if e != nil {
			return e
		}
		current, e := number(raw)
		if e != nil {
			return e
		}
		id := *op
		wanted := *target * mib
		var savedUUID string
		var done int
		if id != "" {
			e = db.QueryRow("SELECT uuid,target,done FROM operations WHERE id=?", id).Scan(&savedUUID, &wanted, &done)
			if e != nil && e != sql.ErrNoRows {
				return e
			}
			if e == nil && (savedUUID != f.UUID || (*target != 0 && wanted != *target*mib)) {
				return errors.New("operation identity or target conflict")
			}
			if e == nil && done == 1 {
				json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": true, "operation": id, "target_bytes": wanted, "already_complete": true})
				return nil
			}
		}
		var pending string
		var pendingTarget int64
		pe := db.QueryRow("SELECT id,target FROM operations WHERE done=0 ORDER BY rowid LIMIT 1").Scan(&pending, &pendingTarget)
		if pe != nil && pe != sql.ErrNoRows {
			return pe
		}
		if pe == nil {
			if id != "" && id != pending {
				return errors.New("another operation needs reconciliation")
			}
			id = pending
			wanted = pendingTarget
		}
		if pe == sql.ErrNoRows {
			if wanted == 0 {
				var st syscall.Statfs_t
				if e = syscall.Statfs(f.Mount, &st); e != nil {
					return e
				}
				used := 100 * float64(st.Blocks-st.Bfree) / float64(st.Blocks)
				if used < float64(*threshold) {
					json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": true, "action": "below_threshold", "usage_percent": used})
					return nil
				}
				wanted = current + 500*mib
			}
			wanted = (wanted + 4*mib - 1) / (4 * mib) * (4 * mib)
			if wanted < current {
				return errors.New("target would shrink volume")
			}
			if wanted > *maximum*mib {
				return errors.New("allocation cap reached")
			}
			raw, e := run("vgs", "--devices", devices, "--noheadings", "--units", "b", "--nosuffix", "-o", "vg_free", "edgelab_r2")
			if e != nil {
				return e
			}
			free, e := number(raw)
			if e != nil {
				return e
			}
			if wanted-current+*reserve*mib > free {
				return errors.New("insufficient backing space")
			}
			if id == "" {
				id = fmt.Sprintf("growth-%d", time.Now().UnixNano())
			}
			if _, e = db.Exec("INSERT INTO operations(id,uuid,target) VALUES(?,?,?)", id, f.UUID, wanted); e != nil {
				return e
			}
			stop("intent")
		}
		if wanted > *maximum*mib {
			return errors.New("pending target exceeds configured cap")
		}
		if current < wanted {
			if _, e = run("lvextend", "--devices", devices, "-L", fmt.Sprintf("%dB", wanted), f.LV); e != nil {
				return e
			}
		}
		stop("lv")
		if _, e = run("resize2fs", f.LV); e != nil {
			return e
		}
		stop("filesystem")
		raw, e = lvm("lvs", "lv_size")
		if e != nil {
			return e
		}
		actual, e := number(raw)
		if e != nil || actual < wanted {
			return errors.New("LV did not reach target")
		}
		raw, e = run("dumpe2fs", "-h", f.LV)
		if e != nil {
			return e
		}
		var blocks, blockSize int64
		for _, line := range strings.Split(raw, "\n") {
			if strings.HasPrefix(line, "Block count:") {
				blocks, _ = number(strings.TrimPrefix(line, "Block count:"))
			}
			if strings.HasPrefix(line, "Block size:") {
				blockSize, _ = number(strings.TrimPrefix(line, "Block size:"))
			}
		}
		if blocks*blockSize < wanted {
			return errors.New("filesystem did not reach target")
		}
		if _, e = db.Exec("UPDATE operations SET done=1 WHERE id=?", id); e != nil {
			return e
		}
		stop("complete")
		return json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": true, "operation": id, "target_bytes": wanted, "lv_bytes": actual, "filesystem_bytes": blocks * blockSize, "sqlite_journal": journal, "sqlite_synchronous": sync})
	}
	for {
		if e = step(); e != nil {
			fail(e)
		}
		if !*watch {
			return
		}
		time.Sleep(2 * time.Second)
	}
}
