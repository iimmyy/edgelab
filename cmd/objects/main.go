package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

type store struct {
	root, owner string
	slots       chan struct{}
}

func (s *store) owned() error {
	var here, parent syscall.Stat_t
	if err := syscall.Stat(s.root, &here); err != nil {
		return err
	}
	if err := syscall.Stat(filepath.Dir(s.root), &parent); err != nil {
		return err
	}
	if here.Dev == parent.Dev {
		return errors.New("object volume is not mounted")
	}
	b, err := os.ReadFile(filepath.Join(s.root, ".owner"))
	if err != nil {
		return err
	}
	if strings.TrimSpace(string(b)) != s.owner {
		return errors.New("volume owner mismatch")
	}
	return nil
}
func syncDir(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}
func (s *store) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	select {
	case s.slots <- struct{}{}:
		defer func() { <-s.slots }()
	default:
		http.Error(w, "busy", 503)
		return
	}
	if err := s.owned(); err != nil {
		http.Error(w, err.Error(), 503)
		return
	}
	w.Header().Set("X-Instance", s.owner)
	if r.URL.Path == "/health" {
		json.NewEncoder(w).Encode(map[string]string{"owner": s.owner})
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/objects/")
	decoded, err := hex.DecodeString(id)
	if !strings.HasPrefix(r.URL.Path, "/objects/") || err != nil || len(decoded) != 32 || id != strings.ToLower(id) {
		http.Error(w, "invalid object ID", 400)
		return
	}
	path := filepath.Join(s.root, id)
	switch r.Method {
	case "GET":
		f, err := os.Open(path)
		if errors.Is(err, os.ErrNotExist) {
			http.NotFound(w, r)
			return
		}
		if err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		defer f.Close()
		info, err := f.Stat()
		if err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		w.Header().Set("Content-Length", fmt.Sprint(info.Size()))
		_, _ = io.Copy(w, f)
	case "PUT":
		f, err := os.CreateTemp(s.root, ".pending-")
		if err != nil {
			http.Error(w, err.Error(), 507)
			return
		}
		defer os.Remove(f.Name())
		defer f.Close()
		h := sha256.New()
		_, err = io.Copy(io.MultiWriter(f, h), http.MaxBytesReader(w, r.Body, 8<<20))
		if err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		if hex.EncodeToString(h.Sum(nil)) != id {
			http.Error(w, "hash mismatch", 422)
			return
		}
		if err = f.Sync(); err == nil {
			err = f.Close()
		}
		if err != nil {
			http.Error(w, err.Error(), 507)
			return
		}
		if err = os.Link(f.Name(), path); errors.Is(err, os.ErrExist) {
			existing, e := os.Open(path)
			if e != nil {
				err = e
			} else {
				sum := sha256.New()
				_, e = io.Copy(sum, existing)
				existing.Close()
				if e != nil {
					err = e
				} else if hex.EncodeToString(sum.Sum(nil)) != id {
					err = errors.New("existing object corrupted")
				} else {
					err = nil
				}
			}
		}
		if err == nil {
			err = os.Remove(f.Name())
		}
		if err == nil {
			err = syncDir(s.root)
		}
		if err != nil {
			http.Error(w, err.Error(), 507)
			return
		}
		w.WriteHeader(http.StatusCreated)
	default:
		w.Header().Set("Allow", "GET, PUT")
		http.Error(w, "method not allowed", 405)
	}
}
func main() {
	root := flag.String("root", "", "mounted object volume")
	owner := flag.String("owner", "", "volume owner")
	addr := flag.String("listen", "127.0.0.1:9000", "application address")
	management := flag.String("management", "127.0.0.1:9001", "health address")
	flag.Parse()
	if *root == "" || *owner == "" {
		log.Fatal("root and owner required")
	}
	s := &store{filepath.Clean(*root), *owner, make(chan struct{}, 16)}
	if err := s.owned(); err != nil {
		log.Fatal(err)
	}
	serve := func(addr string, h http.Handler) {
		server := &http.Server{Addr: addr, Handler: h, ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 10 * time.Second, MaxHeaderBytes: 8192}
		log.Fatal(server.ListenAndServe())
	}
	health := http.NewServeMux()
	health.Handle("/health", s)
	go serve(*management, health)
	serve(*addr, s)
}
