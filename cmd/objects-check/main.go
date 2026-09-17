package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"os"
	"time"
)

type object struct {
	ID    string `json:"id"`
	Bytes int    `json:"bytes"`
}

func main() {
	url := flag.String("url", "http://10.77.0.2:8101", "service URL")
	manifest := flag.String("manifest", "objects.jsonl", "client-owned acknowledgements")
	count := flag.Int("count", 1, "write count")
	size := flag.Int("bytes", 65536, "object bytes")
	seed := flag.Int64("seed", 1, "payload seed")
	verify := flag.Bool("verify", false, "verify every acknowledged object")
	flag.Parse()
	if *size < 1 || *size > 8<<20 || *count < 1 || *count > 10000 {
		panic("invalid workload bounds")
	}
	client := http.Client{Timeout: 4 * time.Second}
	out := json.NewEncoder(os.Stdout)
	failed := false
	check := func(o object, body []byte) {
		start := time.Now()
		method := "GET"
		if !*verify {
			method = "PUT"
		}
		req, err := http.NewRequest(method, *url+"/objects/"+o.ID, bytes.NewReader(body))
		stage := "request"
		if err == nil {
			var resp *http.Response
			resp, err = client.Do(req)
			if err == nil {
				stage = "response"
				defer resp.Body.Close()
				if (*verify && resp.StatusCode != 200) || (!*verify && resp.StatusCode != 201) {
					err = fmt.Errorf("HTTP %d", resp.StatusCode)
				} else if *verify {
					h := sha256.New()
					var n int64
					n, err = io.Copy(h, io.LimitReader(resp.Body, int64(o.Bytes)+1))
					if err == nil && (n != int64(o.Bytes) || hex.EncodeToString(h.Sum(nil)) != o.ID) {
						err = fmt.Errorf("content mismatch")
					}
				}
			}
		}
		record := map[string]any{"id": o.ID, "bytes": o.Bytes, "method": method, "elapsed_ms": float64(time.Since(start).Microseconds()) / 1000, "ok": err == nil, "stage": stage}
		if err != nil {
			record["error"] = err.Error()
			var timeout net.Error
			record["timeout"] = errors.As(err, &timeout) && timeout.Timeout()
			failed = true
		}
		out.Encode(record)
		if err == nil && !*verify {
			f, e := os.OpenFile(*manifest, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
			if e != nil {
				panic(e)
			}
			if e = json.NewEncoder(f).Encode(o); e == nil {
				e = f.Sync()
			}
			f.Close()
			if e != nil {
				panic(e)
			}
		}
	}
	if *verify {
		f, err := os.Open(*manifest)
		if err != nil {
			panic(err)
		}
		defer f.Close()
		d := json.NewDecoder(f)
		n := 0
		for {
			var o object
			err = d.Decode(&o)
			if err == io.EOF {
				break
			}
			if err != nil {
				panic(err)
			}
			n++
			check(o, nil)
		}
		if n == 0 {
			panic("empty acknowledgement manifest")
		}
	} else {
		rng := rand.New(rand.NewSource(*seed))
		for i := 0; i < *count; i++ {
			body := make([]byte, *size)
			rng.Read(body)
			h := sha256.Sum256(body)
			check(object{hex.EncodeToString(h[:]), len(body)}, body)
		}
	}
	if failed {
		os.Exit(1)
	}
}
