package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type result struct {
	Identity map[string]string `json:"identity,omitempty"`
	ID       int64             `json:"id"`
	Started  string            `json:"started"`
	Millis   float64           `json:"elapsed_ms"`
	Instance string            `json:"instance,omitempty"`
	Error    string            `json:"error,omitempty"`
	Timeout  bool              `json:"timeout"`
}

func request(address, app string, instances map[string]bool, expectedIdentities map[string]map[string]string, size int, deadline time.Duration, id int64) result {
	start := time.Now()
	r := result{ID: id, Started: start.UTC().Format(time.RFC3339Nano)}
	err := func() error {
		conn, err := net.DialTimeout("tcp", address, deadline)
		if err != nil {
			return err
		}
		defer conn.Close()
		conn.SetDeadline(start.Add(deadline))
		payload := make([]byte, size)
		for i := range payload {
			payload[i] = byte((int(id) + i) % 251)
		}
		written := make(chan error, 1)
		go func() {
			_, err := io.Copy(conn, bytes.NewReader(payload))
			if err == nil {
				err = conn.(*net.TCPConn).CloseWrite()
			}
			written <- err
		}()
		reader := bufio.NewReaderSize(conn, 4096)
		line, err := reader.ReadSlice('\n')
		if err != nil {
			return err
		}
		var identity map[string]string
		if err := json.Unmarshal(line, &identity); err != nil {
			return err
		}
		r.Instance = identity["instance"]
		r.Identity = identity
		if identity["app"] != app || !instances[r.Instance] {
			return fmt.Errorf("unexpected identity: %s", line)
		}
		for _, key := range []string{"worker", "region", "version"} {
			if identity[key] == "" {
				return fmt.Errorf("missing identity field %s", key)
			}
		}
		if expectedIdentities != nil {
			expected, ok := expectedIdentities[r.Instance]
			if !ok {
				return fmt.Errorf("instance absent from independent deployment manifest")
			}
			for _, key := range []string{"app", "worker", "region", "version"} {
				if identity[key] != expected[key] {
					return fmt.Errorf("identity %s mismatch: got %q want %q", key, identity[key], expected[key])
				}
			}
		}
		expected := append(payload, []byte("\nEOF\n")...)
		actual, err := io.ReadAll(io.LimitReader(reader, int64(len(expected)+1)))
		if err != nil {
			return err
		}
		if err := <-written; err != nil {
			return err
		}
		if !bytes.Equal(expected, actual) {
			return fmt.Errorf("payload mismatch: received %d expected %d", len(actual), len(expected))
		}
		return nil
	}()
	r.Millis = float64(time.Since(start).Microseconds()) / 1000
	if err != nil {
		r.Error = err.Error()
		if e, ok := err.(net.Error); ok {
			r.Timeout = e.Timeout()
		}
	}
	return r
}

func main() {
	address := flag.String("address", "127.0.0.1:8101", "destination")
	app := flag.String("app", "echo", "expected application")
	allowed := flag.String("instances", "echo-a,echo-b", "allowed instance identities")
	count := flag.Int64("count", 100, "maximum attempts; capped at four million")
	concurrency := flag.Int("concurrency", 10, "parallel requests, at most 256")
	size := flag.Int("bytes", 1024, "payload bytes, at most 1 MiB")
	duration := flag.Duration("duration", 0, "stop starting requests after this duration")
	deadline := flag.Duration("timeout", 3*time.Second, "whole-request deadline")
	history := flag.String("history", "", "optional JSONL attempt history")
	expectedPath := flag.String("expected-identities", "", "independent deployment identity JSON")
	flag.Parse()
	if *count < 1 || *count > 4000000 || *concurrency < 1 || *concurrency > 256 || *size < 0 || *size > 1048576 || *deadline <= 0 || *duration < 0 {
		fmt.Fprintln(os.Stderr, "workload exceeds supported bounds")
		os.Exit(2)
	}
	instances := map[string]bool{}
	for _, s := range strings.Split(*allowed, ",") {
		instances[s] = true
	}
	var expectedIdentities map[string]map[string]string
	if *expectedPath != "" {
		data, err := os.ReadFile(*expectedPath)
		if err != nil || len(data) > 65536 {
			fmt.Fprintln(os.Stderr, "cannot read bounded identity manifest")
			os.Exit(2)
		}
		if err = json.Unmarshal(data, &expectedIdentities); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		for id := range instances {
			for _, key := range []string{"app", "worker", "region", "version"} {
				if expectedIdentities[id][key] == "" {
					fmt.Fprintln(os.Stderr, "incomplete identity manifest")
					os.Exit(2)
				}
			}
		}
	}
	var out *os.File
	if *history != "" {
		var err error
		out, err = os.Create(*history)
		if err != nil {
			panic(err)
		}
		defer out.Close()
	}
	start := time.Now()
	var next atomic.Int64
	results := make(chan result, *concurrency)
	var workers sync.WaitGroup
	for i := 0; i < *concurrency; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for {
				id := next.Add(1)
				if id > *count || (*duration > 0 && time.Since(start) >= *duration) {
					return
				}
				results <- request(*address, *app, instances, expectedIdentities, *size, *deadline, id)
			}
		}()
	}
	go func() { workers.Wait(); close(results) }()
	latencies := make([]float64, 0)
	successes, failures, timeouts := 0, 0, 0
	backends := map[string]int{}
	identities := map[string]map[string]string{}
	encoder := json.NewEncoder(out)
	for r := range results {
		latencies = append(latencies, r.Millis)
		if r.Error == "" {
			successes++
			backends[r.Instance]++
			identities[r.Instance] = r.Identity
		} else {
			failures++
		}
		if r.Timeout {
			timeouts++
		}
		if out != nil {
			if err := encoder.Encode(r); err != nil {
				panic(err)
			}
		}
	}
	elapsed := time.Since(start).Seconds()
	sort.Float64s(latencies)
	percentile := func(p float64) float64 {
		if len(latencies) == 0 {
			return 0
		}
		return latencies[int(float64(len(latencies)-1)*p)]
	}
	json.NewEncoder(os.Stdout).Encode(map[string]any{"attempts": len(latencies), "successes": successes, "failures": failures, "timeouts": timeouts, "seconds": elapsed, "successes_per_second": float64(successes) / elapsed, "payload_bytes": *size, "concurrency": *concurrency, "latency_population": "all attempts including failures and timeouts", "p50_ms": percentile(.5), "p95_ms": percentile(.95), "p99_ms": percentile(.99), "max_ms": percentile(1), "backends": backends, "identities": identities, "measurement": "application round-trip time including connection setup, payload transfer and server work"})
	if failures > 0 || successes == 0 {
		os.Exit(1)
	}
}
