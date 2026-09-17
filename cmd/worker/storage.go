package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

type pool struct {
	Machine string `json:"machine"`
	Device  string `json:"device"`
	UUID    string `json:"uuid"`
}
type volume struct {
	Name       string `json:"lv_name"`
	UUID       string `json:"lv_uuid"`
	Tags       string `json:"lv_tags"`
	Pool       string `json:"pool_lv"`
	Origin     string `json:"origin"`
	Attributes string `json:"lv_attr"`
	Size       string `json:"lv_size"`
	Data       string `json:"data_percent"`
	Metadata   string `json:"metadata_percent"`
}
type storage struct {
	root                     string
	pool                     pool
	dataLimit, metadataLimit float64
}

func command(name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	b, e := exec.CommandContext(ctx, name, args...).CombinedOutput()
	if e != nil {
		return "", fmt.Errorf("%s: %w: %s", name, e, b)
	}
	return strings.TrimSpace(string(b)), nil
}
func (s storage) inspect(name string) (volume, error) {
	var v volume
	data, e := command("lvs", "--devices", s.pool.Device, "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "lv_name,lv_uuid,lv_tags,pool_lv,origin,lv_attr,lv_size,data_percent,metadata_percent", "edgelab_worker")
	if e != nil {
		return v, e
	}
	var r struct {
		Report []struct {
			LV []volume `json:"lv"`
		} `json:"report"`
	}
	if e = json.Unmarshal([]byte(data), &r); e != nil {
		return v, e
	}
	for _, group := range r.Report {
		for _, item := range group.LV {
			if item.Name == name {
				return item, nil
			}
		}
	}
	return v, os.ErrNotExist
}
func (s storage) capacity(reserve int64) error {
	p, e := s.inspect("pool")
	if e != nil {
		return e
	}
	if p.UUID != s.pool.UUID {
		return errors.New("thinpool identity mismatch")
	}
	data, e := strconv.ParseFloat(p.Data, 64)
	if e != nil {
		return e
	}
	metadata, e := strconv.ParseFloat(p.Metadata, 64)
	if e != nil {
		return e
	}
	size, e := strconv.ParseFloat(p.Size, 64)
	if e != nil {
		return e
	}
	if data+100*float64(reserve)/size >= s.dataLimit {
		return fmt.Errorf("thinpool data threshold: %.3f percent, limit %.3f", data, s.dataLimit)
	}
	if metadata+5 >= s.metadataLimit {
		return fmt.Errorf("thinpool metadata threshold: %.3f percent, limit %.3f", metadata, s.metadataLimit)
	}
	return nil
}
func (s storage) validate(v volume, tag, origin string) error {
	tags := strings.Split(v.Tags, ",")
	sort.Strings(tags)
	if !contains(tags, "edgelab_worker") || !contains(tags, tag) || v.Pool != "pool" || v.Origin != origin {
		return errors.New("volume identity conflict")
	}
	size, e := strconv.ParseFloat(v.Size, 64)
	if e != nil || size != 256<<20 {
		return errors.New("volume size conflict")
	}
	return nil
}
func contains(items []string, want string) bool {
	for _, item := range items {
		if item == want {
			return true
		}
	}
	return false
}
func (s storage) create(name, tag, origin string) (volume, error) {
	v, e := s.inspect(name)
	if e == nil {
		return v, s.validate(v, tag, origin)
	}
	if !os.IsNotExist(e) {
		return v, e
	}
	if e = s.capacity(64 << 20); e != nil {
		return v, e
	}
	args := []string{"--devices", s.pool.Device, "--addtag", "edgelab_worker", "--addtag", tag, "-n", name}
	if origin == "" {
		args = append(args, "-V", "256M", "-T", "edgelab_worker/pool")
	} else {
		args = append(args, "-s", "edgelab_worker/"+origin)
	}
	if _, e = command("lvcreate", args...); e != nil {
		return v, e
	}
	v, e = s.inspect(name)
	if e != nil {
		return v, e
	}
	return v, s.validate(v, tag, origin)
}
func (s storage) mount(name string, readonly bool) (string, error) {
	root := filepath.Join(s.root, "mounts", name)
	if e := os.MkdirAll(root, 0700); e != nil {
		return "", e
	}
	existing, e := command("findmnt", "--noheadings", "--mountpoint", root, "-o", "SOURCE")
	if e == nil {
		expected, e := command("readlink", "-f", "/dev/edgelab_worker/"+name)
		if e != nil {
			return "", e
		}
		actual, e := command("readlink", "-f", existing)
		if e != nil || actual != expected {
			return "", errors.New("mount identity conflict")
		}
		return root, nil
	}
	options := "rw,nosuid,nodev,noexec"
	if readonly {
		options = "ro,nosuid,nodev,noexec"
	}
	if _, e = command("mount", "-o", options, "/dev/edgelab_worker/"+name, root); e != nil {
		return "", e
	}
	return root, nil
}
func (s storage) unmount(name string) error {
	root := filepath.Join(s.root, "mounts", name)
	if _, e := command("findmnt", "--mountpoint", root); e != nil {
		return nil
	}
	_, e := command("umount", root)
	return e
}
func treeDigest(root string) (string, error) {
	records := map[string]string{}
	e := filepath.WalkDir(root, func(p string, d os.DirEntry, e error) error {
		if e != nil {
			return e
		}
		rel, e := filepath.Rel(root, p)
		if e != nil {
			return e
		}
		if rel == "." {
			return nil
		}
		if rel == "lost+found" {
			return filepath.SkipDir
		}
		if rel == ".edgelab-image.json" {
			return nil
		}
		info, e := d.Info()
		if e != nil {
			return e
		}
		if d.IsDir() {
			records[rel] = fmt.Sprintf("directory:%o", info.Mode().Perm())
			return nil
		}
		if !info.Mode().IsRegular() {
			return errors.New("unexpected filesystem entry")
		}
		b, e := os.ReadFile(p)
		if e != nil {
			return e
		}
		records[rel] = fmt.Sprintf("%o:%s", info.Mode().Perm(), digest(b))
		return nil
	})
	if e != nil {
		return "", e
	}
	data, e := json.Marshal(records)
	if e != nil {
		return "", e
	}
	return digest(data), nil
}
