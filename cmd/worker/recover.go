package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

type acceptedOwner struct {
	Snapshot routeSnapshot `json:"snapshot"`
	Recovery *struct {
		Operation string `json:"operation"`
		Digest    string `json:"digest"`
	} `json:"recovery"`
}
type recoveryJournal struct {
	Operation        string                 `json:"operation"`
	Owner            string                 `json:"owner"`
	Database         string                 `json:"database"`
	Snapshot         *routeSnapshot         `json:"snapshot"`
	Credential       string                 `json:"credential"`
	Quarantined      map[string]routeRecord `json:"quarantined"`
	Acknowledgements map[string]string      `json:"acknowledgements"`
	Complete         bool                   `json:"complete"`
}

func recoverWorker(args []string) error {
	flags := flag.NewFlagSet("recover", flag.ContinueOnError)
	configPath := flags.String("config", "", "owner configuration")
	database := flags.String("state", "", "restored database")
	journalPath := flags.String("journal", "", "recovery journal outside the restored backup")
	operation := flags.String("operation", "", "stable administrative recovery identity")
	crash := flags.Bool("crash-after-first-ack", false, "lab process interruption")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if !routeName.MatchString(*operation) || *database == "" || *journalPath == "" || *configPath == "" || *database == *journalPath {
		return errors.New("invalid recovery arguments")
	}
	data, err := os.ReadFile(*configPath)
	if err != nil {
		return err
	}
	var config daemonConfig
	if err = json.Unmarshal(data, &config); err != nil {
		return err
	}
	if len(config.Routers) != 2 || config.Routers[0] == config.Routers[1] || len(config.RouterAdminToken) < 32 || config.RestoreGuard == "" {
		return errors.New("recovery requires both distinct routers and an external guard")
	}
	lock, err := os.OpenFile(*database+".lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return errors.New("stop worker management before restoration")
	}
	if err = durableFile(config.RestoreGuard, []byte(*operation)); err != nil {
		return err
	}
	journal := recoveryJournal{Operation: *operation, Owner: config.Owner, Database: *database, Quarantined: map[string]routeRecord{}, Acknowledgements: map[string]string{}}
	if prior, err := os.ReadFile(*journalPath); err == nil {
		if err = json.Unmarshal(prior, &journal); err != nil {
			return err
		}
		if journal.Operation != *operation || journal.Owner != config.Owner || journal.Database != *database {
			return errors.New("recovery journal identity mismatch")
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	save := func() error {
		data, err := json.Marshal(journal)
		if err != nil {
			return err
		}
		return durableFile(*journalPath, data)
	}
	if err = save(); err != nil {
		return err
	}
	db, err := sql.Open("sqlite3", *database+"?_journal_mode=WAL&_synchronous=FULL&_busy_timeout=2000")
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
	call := func(router string, snapshot *routeSnapshot, hash *string) (acceptedOwner, error) {
		payload := map[string]any{"owner": config.Owner, "operation": *operation, "snapshot": snapshot, "credential_hash": hash}
		data, _ := json.Marshal(payload)
		request, err := http.NewRequest("POST", strings.TrimRight(router, "/")+"/recovery", bytes.NewReader(data))
		if err != nil {
			return acceptedOwner{}, err
		}
		request.Header.Set("Authorization", "Bearer "+config.RouterAdminToken)
		request.Header.Set("Content-Type", "application/json")
		client := http.Client{Timeout: 3 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		response, err := client.Do(request)
		if err != nil {
			return acceptedOwner{}, err
		}
		defer response.Body.Close()
		data, err = io.ReadAll(io.LimitReader(response.Body, 16*1024*1024+1))
		if err != nil {
			return acceptedOwner{}, err
		}
		if len(data) > 16*1024*1024 || response.StatusCode != 200 {
			return acceptedOwner{}, fmt.Errorf("recovery blocked: router status %d", response.StatusCode)
		}
		var owner acceptedOwner
		err = json.Unmarshal(data, &owner)
		return owner, err
	}
	if journal.Snapshot == nil {
		var backup routeSnapshot
		var blob []byte
		if err = db.QueryRow("SELECT body FROM routing_state WHERE id=1").Scan(&blob); err != nil {
			return err
		}
		if err = json.Unmarshal(blob, &backup); err != nil {
			return err
		}
		if backup.Owner != config.Owner {
			return errors.New("restored database belongs to another owner")
		}
		// Freeze every publisher first; collect histories again after both fences exist.
		for _, router := range config.Routers {
			if _, err = call(router, nil, nil); err != nil {
				return err
			}
		}
		merged := routeSnapshot{Schema: 1, Owner: config.Owner, Incarnation: backup.Incarnation, Revision: backup.Revision, Records: map[string]routeRecord{}}
		for _, router := range config.Routers {
			owner, err := call(router, nil, nil)
			if err != nil {
				return err
			}
			if owner.Snapshot.Incarnation > merged.Incarnation {
				merged.Incarnation = owner.Snapshot.Incarnation
			}
			if owner.Snapshot.Revision > merged.Revision {
				merged.Revision = owner.Snapshot.Revision
			}
			for id, record := range owner.Snapshot.Records {
				old, exists := merged.Records[id]
				if exists && (old.App != record.App || old.Endpoint != record.Endpoint) {
					return errors.New("accepted identities conflict; recovery remains quarantined")
				}
				if !exists || record.Revision > old.Revision {
					record.Deleted = record.Deleted || old.Deleted
					merged.Records[id] = record
				} else if record.Deleted {
					old.Deleted = true
					merged.Records[id] = old
				}
			}
		}
		if merged.Incarnation >= 1<<63-1 || merged.Revision >= 1<<63-1 {
			return errors.New("recovery revision exhausted")
		}
		merged.Incarnation++
		merged.Revision++
		for id, old := range backup.Records {
			if accepted, exists := merged.Records[id]; exists && old.Deleted && accepted.App == old.App && accepted.Endpoint == old.Endpoint {
				accepted.Deleted = true
				if old.Revision > accepted.Revision {
					accepted.Revision = old.Revision
				}
				merged.Records[id] = accepted
			}
			accepted, exists := merged.Records[id]
			if !exists || old.App != accepted.App || old.Endpoint != accepted.Endpoint || accepted.Deleted && !old.Deleted {
				journal.Quarantined[id] = old
			}
		}
		credential := make([]byte, 32)
		if _, err = rand.Read(credential); err != nil {
			return err
		}
		journal.Credential = hex.EncodeToString(credential)
		journal.Snapshot = &merged
		if err = save(); err != nil {
			return err
		}
	}
	if len(journal.Credential) != 64 || journal.Snapshot.Owner != config.Owner {
		return errors.New("invalid prepared recovery journal")
	}
	if _, err = hex.DecodeString(journal.Credential); err != nil {
		return err
	}
	sum := sha256.Sum256([]byte(journal.Credential))
	hash := hex.EncodeToString(sum[:])
	var resultDigest string
	for i, router := range config.Routers {
		owner, err := call(router, journal.Snapshot, &hash)
		if err != nil {
			return err
		}
		if owner.Recovery == nil || owner.Recovery.Operation != *operation {
			return errors.New("router did not acknowledge recovery identity")
		}
		if i == 0 {
			resultDigest = owner.Recovery.Digest
		} else if owner.Recovery.Digest != resultDigest {
			return errors.New("routers acknowledged different recovery results")
		}
		journal.Acknowledgements[router] = owner.Recovery.Digest
		if err = save(); err != nil {
			return err
		}
		if i == 0 && *crash {
			os.Exit(77)
		}
	}
	blob, err := json.Marshal(journal.Snapshot)
	if err != nil {
		return err
	}
	updated, err := db.Exec("UPDATE routing_state SET body=? WHERE id=1", blob)
	if err != nil {
		return err
	}
	count, err := updated.RowsAffected()
	if err != nil {
		return err
	}
	if count != 1 {
		return errors.New("restored owner row missing")
	}
	config.Token = journal.Credential
	encoded, err := json.Marshal(config)
	if err != nil {
		return err
	}
	if err = durableFile(*configPath, encoded); err != nil {
		return err
	}
	journal.Complete = true
	if err = save(); err != nil {
		return err
	}
	if err = os.Remove(config.RestoreGuard); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err = syncDirectory(filepath.Dir(config.RestoreGuard)); err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"operation": *operation, "incarnation": journal.Snapshot.Incarnation, "quarantined": len(journal.Quarantined), "acknowledgements": len(journal.Acknowledgements), "complete": true})
}
