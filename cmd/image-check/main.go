package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

func main() {
	root := flag.String("root", "", "mounted image path")
	manifest := flag.String("expected", "", "independent expected filesystem")
	flag.Parse()
	var expected struct {
		Files  map[string]string `json:"files"`
		Absent []string          `json:"absent"`
	}
	b, e := os.ReadFile(*manifest)
	if e != nil {
		panic(e)
	}
	if e = json.Unmarshal(b, &expected); e != nil {
		panic(e)
	}
	if *root == "" || len(expected.Files) == 0 {
		panic("expected files and root required")
	}
	results := map[string]any{}
	failed := false
	for name, want := range expected.Files {
		f, e := os.Open(filepath.Join(*root, name))
		got := ""
		if e == nil {
			h := sha256.New()
			var n int64
			n, e = io.Copy(h, io.LimitReader(f, (64<<20)+1))
			if e == nil && n > 64<<20 {
				e = errors.New("file exceeds supported size")
			}
			f.Close()
			got = hex.EncodeToString(h.Sum(nil))
		}
		ok := e == nil && got == want
		results[name] = map[string]any{"ok": ok, "sha256": got}
		if !ok {
			failed = true
		}
	}
	for _, name := range expected.Absent {
		_, e := os.Lstat(filepath.Join(*root, name))
		ok := errors.Is(e, os.ErrNotExist)
		results[name] = map[string]any{"absent": ok}
		if !ok {
			failed = true
		}
	}
	e = filepath.WalkDir(*root, func(path string, d os.DirEntry, e error) error {
		if e != nil {
			return e
		}
		rel, e := filepath.Rel(*root, path)
		if e != nil {
			return e
		}
		if rel == "lost+found" {
			return filepath.SkipDir
		}
		if rel == ".edgelab-image.json" || d.IsDir() {
			return nil
		}
		if d.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("unexpected symlink %s", rel)
		}
		if _, ok := expected.Files[rel]; !ok {
			return fmt.Errorf("unexpected file %s", rel)
		}
		return nil
	})
	if e != nil {
		results["inventory_error"] = e.Error()
		failed = true
	}
	json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": !failed, "root": *root, "files": results})
	if failed {
		os.Exit(1)
	}
}
