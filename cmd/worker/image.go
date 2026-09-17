package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const layerType = "application/vnd.oci.image.layer.v1.tar+gzip"
const maxBlob = 16 << 20
const maxExpanded = 64 << 20

type descriptor struct {
	MediaType string `json:"mediaType"`
	Digest    string `json:"digest"`
	Size      int64  `json:"size"`
}
type manifest struct {
	SchemaVersion int          `json:"schemaVersion"`
	MediaType     string       `json:"mediaType"`
	Config        descriptor   `json:"config"`
	Layers        []descriptor `json:"layers"`
}
type entry struct {
	name      string
	directory bool
	mode      os.FileMode
	data      []byte
	modified  time.Time
}

func digest(data []byte) string { h := sha256.Sum256(data); return hex.EncodeToString(h[:]) }
func validDigest(s string) bool {
	b, e := hex.DecodeString(strings.TrimPrefix(s, "sha256:"))
	return strings.HasPrefix(s, "sha256:") && e == nil && len(b) == 32 && s == strings.ToLower(s)
}
func syncDirectory(root string) error {
	f, e := os.Open(root)
	if e != nil {
		return e
	}
	defer f.Close()
	return f.Sync()
}
func durableFile(name string, data []byte) error {
	f, e := os.CreateTemp(filepath.Dir(name), ".pending-")
	if e != nil {
		return e
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if _, e = f.Write(data); e != nil {
		return e
	}
	if e = f.Sync(); e != nil {
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	if e = os.Rename(f.Name(), name); e != nil {
		return e
	}
	return syncDirectory(filepath.Dir(name))
}
func fetch(store, cache string, d descriptor) ([]byte, error) {
	if !validDigest(d.Digest) || d.Size < 1 || d.Size > maxBlob {
		return nil, errors.New("invalid descriptor")
	}
	id := strings.TrimPrefix(d.Digest, "sha256:")
	target := filepath.Join(cache, id)
	if data, e := os.ReadFile(target); e == nil {
		if int64(len(data)) == d.Size && digest(data) == id {
			return data, nil
		}
		return nil, errors.New("corrupt cached blob")
	}
	var cached int64
	files, e := os.ReadDir(cache)
	if e != nil {
		return nil, e
	}
	for _, f := range files {
		info, e := f.Info()
		if e != nil {
			return nil, e
		}
		cached += info.Size()
	}
	if cached+d.Size > 512<<20 {
		return nil, errors.New("blob cache capacity reached")
	}
	client := http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirect not supported") }}
	resp, e := client.Get(strings.TrimRight(store, "/") + "/blobs/sha256/" + id)
	if e != nil {
		return nil, fmt.Errorf("fetch: %w", e)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("fetch HTTP %d", resp.StatusCode)
	}
	data, e := io.ReadAll(io.LimitReader(resp.Body, d.Size+1))
	if e != nil {
		return nil, e
	}
	if int64(len(data)) != d.Size {
		return nil, errors.New("blob length mismatch")
	}
	if digest(data) != id {
		return nil, errors.New("blob digest mismatch")
	}
	if e = durableFile(target, data); e != nil {
		return nil, e
	}
	return data, nil
}
func loadLayers(store, cache string, desc descriptor) (manifest, [][]entry, error) {
	var m manifest
	data, e := fetch(store, cache, desc)
	if e != nil {
		return m, nil, e
	}
	faultPoint("manifest")
	if e = json.Unmarshal(data, &m); e != nil {
		return m, nil, e
	}
	if m.SchemaVersion != 2 || m.MediaType != "application/vnd.oci.image.manifest.v1+json" || len(m.Layers) < 1 || len(m.Layers) > 4 || m.Config.MediaType != "application/vnd.oci.image.config.v1+json" {
		return m, nil, errors.New("unsupported image manifest")
	}
	config, e := fetch(store, cache, m.Config)
	if e != nil {
		return m, nil, e
	}
	faultPoint("config")
	var c struct {
		Architecture string `json:"architecture"`
		OS           string `json:"os"`
		RootFS       struct {
			Type    string   `json:"type"`
			DiffIDs []string `json:"diff_ids"`
		} `json:"rootfs"`
	}
	if e = json.Unmarshal(config, &c); e != nil {
		return m, nil, e
	}
	if c.Architecture != "arm64" || c.OS != "linux" || c.RootFS.Type != "layers" || len(c.RootFS.DiffIDs) != len(m.Layers) {
		return m, nil, errors.New("unsupported image configuration")
	}
	layers := [][]entry{}
	total := int64(0)
	count := 0
	for i, d := range m.Layers {
		if d.MediaType != layerType {
			return m, nil, errors.New("unsupported layer media type")
		}
		blob, e := fetch(store, cache, d)
		if e != nil {
			return m, nil, e
		}
		faultPoint(fmt.Sprintf("layer-%d", i))
		gz, e := gzip.NewReader(bytes.NewReader(blob))
		if e != nil {
			return m, nil, e
		}
		plain, e := io.ReadAll(io.LimitReader(gz, maxExpanded+1))
		closeErr := gz.Close()
		if e != nil {
			return m, nil, e
		}
		if closeErr != nil {
			return m, nil, closeErr
		}
		if len(plain) > maxExpanded {
			return m, nil, errors.New("expanded size limit")
		}
		if !validDigest(c.RootFS.DiffIDs[i]) || "sha256:"+digest(plain) != c.RootFS.DiffIDs[i] {
			return m, nil, errors.New("diff ID mismatch")
		}
		reader := tar.NewReader(bytes.NewReader(plain))
		entries := []entry{}
		seen := map[string]bool{}
		for {
			h, e := reader.Next()
			if e == io.EOF {
				break
			}
			if e != nil {
				return m, nil, e
			}
			count++
			if count > 4096 {
				return m, nil, errors.New("file count limit")
			}
			name := strings.TrimSuffix(h.Name, "/")
			if name == "." || name == "" {
				if h.Typeflag == tar.TypeDir {
					continue
				}
				return m, nil, errors.New("invalid root entry")
			}
			if path.IsAbs(name) || strings.Contains(name, "\\") {
				return m, nil, errors.New("path escape")
			}
			for _, part := range strings.Split(name, "/") {
				if part == ".." {
					return m, nil, errors.New("path escape")
				}
			}
			name = path.Clean(name)
			if name == "." || name == "lost+found" || strings.HasPrefix(name, "lost+found/") || strings.HasPrefix(name, ".edgelab") || seen[name] {
				return m, nil, errors.New("reserved or duplicate path")
			}
			seen[name] = true
			if h.Typeflag != tar.TypeReg && h.Typeflag != tar.TypeDir {
				return m, nil, errors.New("unsupported link or special file")
			}
			if h.Uid != 0 || h.Gid != 0 || h.Mode&07000 != 0 || len(h.PAXRecords) > 0 {
				return m, nil, errors.New("unsupported file attributes")
			}
			if h.Size < 0 || h.Size > maxExpanded {
				return m, nil, errors.New("expanded size limit")
			}
			total += h.Size
			if total > maxExpanded {
				return m, nil, errors.New("expanded size limit")
			}
			content, e := io.ReadAll(reader)
			if e != nil {
				return m, nil, e
			}
			if int64(len(content)) != h.Size {
				return m, nil, errors.New("truncated entry")
			}
			if strings.HasPrefix(path.Base(name), ".wh.") && (h.Typeflag != tar.TypeReg || h.Size != 0) {
				return m, nil, errors.New("invalid whiteout")
			}
			entries = append(entries, entry{name, h.Typeflag == tar.TypeDir, os.FileMode(h.Mode) & 0777, content, h.ModTime})
		}
		layers = append(layers, entries)
	}
	return m, layers, nil
}
func applyLayers(layers [][]entry) error {
	for _, entries := range layers {
		for _, item := range entries {
			base := path.Base(item.name)
			parent := path.Dir(item.name)
			if base == ".wh..wh..opq" {
				children, e := os.ReadDir(parent)
				if e != nil && !os.IsNotExist(e) {
					return e
				}
				for _, child := range children {
					if e = os.RemoveAll(path.Join(parent, child.Name())); e != nil {
						return e
					}
				}
			} else if strings.HasPrefix(base, ".wh.") {
				target := strings.TrimPrefix(base, ".wh.")
				if target == "" || target == "." || target == ".." {
					return errors.New("invalid whiteout target")
				}
				if e := os.RemoveAll(path.Join(parent, target)); e != nil {
					return e
				}
			}
		}
		for _, item := range entries {
			if strings.HasPrefix(path.Base(item.name), ".wh.") {
				continue
			}
			if e := os.MkdirAll(path.Dir(item.name), 0755); e != nil {
				return e
			}
			if info, e := os.Lstat(item.name); e == nil && info.IsDir() != item.directory {
				if e = os.RemoveAll(item.name); e != nil {
					return e
				}
			}
			if item.directory {
				if e := os.MkdirAll(item.name, 0755); e != nil {
					return e
				}
			} else {
				f, e := os.OpenFile(item.name, os.O_CREATE|os.O_TRUNC|os.O_WRONLY|syscall.O_NOFOLLOW, 0600)
				if e != nil {
					return e
				}
				_, e = f.Write(item.data)
				if e == nil {
					e = f.Sync()
				}
				closeErr := f.Close()
				if e != nil {
					return e
				}
				if closeErr != nil {
					return closeErr
				}
				faultPoint("unpack-partial")
			}
		}
		// Directory metadata follows children so restricted modes cannot block extraction.
		for i := len(entries) - 1; i >= 0; i-- {
			item := entries[i]
			if strings.HasPrefix(path.Base(item.name), ".wh.") {
				continue
			}
			if e := os.Chmod(item.name, item.mode); e != nil {
				return e
			}
			if e := os.Chtimes(item.name, item.modified, item.modified); e != nil {
				return e
			}
		}
	}
	return nil
}
