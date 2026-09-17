package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"time"
)

func convergence(binary, dir, token string) error {
	nodes := make([]verifier, 2)
	children := []*exec.Cmd{}
	logs := []*os.File{}
	defer func() {
		for _, child := range children {
			child.Process.Kill()
			child.Wait()
		}
		for _, log := range logs {
			log.Close()
		}
	}()
	used := map[string]bool{}
	for i := range nodes {
		var address string
		for {
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				return err
			}
			address = listener.Addr().String()
			listener.Close()
			if !used[address] {
				used[address] = true
				break
			}
		}
		log, err := os.Create(filepath.Join(dir, fmt.Sprintf("convergence-%d.log", i)))
		if err != nil {
			return err
		}
		logs = append(logs, log)
		child := exec.Command(binary, "--config", filepath.Join(dir, "config.json"), "--state", filepath.Join(dir, fmt.Sprintf("convergence-%d.state", i)), "--listen", address)
		child.Stdout = log
		child.Stderr = log
		if err = child.Start(); err != nil {
			return err
		}
		children = append(children, child)
		nodes[i] = verifier{client: &http.Client{Timeout: 3 * time.Second}, address: "http://" + address, token: token}
		ready := false
		for deadline := time.Now().Add(3 * time.Second); time.Now().Before(deadline); {
			if code, _, err := nodes[i].request("GET", "/health", "", nil); err == nil && code == 200 {
				ready = true
				break
			}
			time.Sleep(20 * time.Millisecond)
		}
		if !ready {
			return fmt.Errorf("convergence node %d did not start", i)
		}
	}
	first := snapshot{Schema: 1, Owner: "worker-1", Incarnation: 1, Revision: 1, Records: map[string]record{"one": {ID: "one", App: "echo", Endpoint: "127.0.0.1:19001", Revision: 1}}}
	second := snapshot{Schema: 1, Owner: "worker-1", Incarnation: 1, Revision: 2, Records: map[string]record{"one": first.Records["one"], "two": {ID: "two", App: "echo", Endpoint: "127.0.0.1:19002", Revision: 2}}}
	final := snapshot{Schema: 1, Owner: "worker-1", Incarnation: 1, Revision: 3, Records: map[string]record{"one": {ID: "one", App: "echo", Endpoint: "127.0.0.1:19001", Revision: 3, Deleted: true}, "two": second.Records["two"]}}
	type step struct {
		Node     int      `json:"node"`
		Snapshot snapshot `json:"snapshot"`
		Status   int      `json:"expected_status"`
		Drop     bool     `json:"drop"`
		DelayMS  int      `json:"delay_ms"`
	}
	steps := []step{{0, first, 200, false, 0}, {1, first, 200, false, 0}, {0, second, 200, false, 0}, {1, second, 0, true, 0}, {0, second, 200, false, 0}, {1, final, 200, false, 50}, {0, final, 200, false, 25}, {0, second, 409, false, 0}, {1, first, 409, false, 10}}
	input, err := json.MarshalIndent(steps, "", "  ")
	if err != nil {
		return err
	}
	if err = os.WriteFile(filepath.Join(dir, "input-history.json"), input, 0600); err != nil {
		return err
	}
	started := time.Now()
	for _, step := range steps {
		if step.Drop {
			continue
		}
		time.Sleep(time.Duration(step.DelayMS) * time.Millisecond)
		if err := nodes[step.Node].publish(step.Snapshot, step.Status); err != nil {
			return err
		}
	}
	hashes := []string{}
	for i := range nodes {
		current, err := nodes[i].current()
		if err != nil {
			return err
		}
		if !reflect.DeepEqual(current.Owners["worker-1"].Snapshot, final) {
			return fmt.Errorf("node %d differs from independent final history", i)
		}
		bytes, _ := json.Marshal(current.Owners["worker-1"].Snapshot)
		sum := sha256.Sum256(bytes)
		hashes = append(hashes, hex.EncodeToString(sum[:]))
	}
	if time.Since(started) > 3*time.Second {
		return fmt.Errorf("convergence exceeded three-second local bound")
	}
	result, _ := json.MarshalIndent(map[string]any{"expected": final, "node_hashes": hashes, "elapsed_ms": time.Since(started).Milliseconds(), "bound_ms": 3000, "all_match": true}, "", "  ")
	return os.WriteFile(filepath.Join(dir, "convergence-report.json"), result, 0600)
}
