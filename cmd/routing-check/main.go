package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

type record struct {
	ID       string `json:"id"`
	App      string `json:"app"`
	Endpoint string `json:"endpoint"`
	Revision uint64 `json:"revision"`
	Deleted  bool   `json:"deleted"`
}
type snapshot struct {
	Schema      int               `json:"schema"`
	Owner       string            `json:"owner"`
	Incarnation uint64            `json:"incarnation"`
	Revision    uint64            `json:"revision"`
	Records     map[string]record `json:"records"`
}
type ownerView struct {
	Snapshot   snapshot `json:"snapshot"`
	Reconciled uint64   `json:"reconciled_ms"`
	Stale      bool     `json:"stale"`
}
type view struct {
	Generation uint64               `json:"generation"`
	Owners     map[string]ownerView `json:"owners"`
}
type observation struct {
	Case      string `json:"case"`
	Passed    bool   `json:"passed"`
	Error     string `json:"error,omitempty"`
	ElapsedMS int64  `json:"elapsed_ms"`
}
type verifier struct {
	client         *http.Client
	address, token string
	observations   []observation
}

func (v *verifier) request(method, path, token string, data any) (int, []byte, error) {
	var body io.Reader
	if data != nil {
		bytes, err := json.Marshal(data)
		if err != nil {
			return 0, nil, err
		}
		body = bytesReader(bytes)
	}
	req, err := http.NewRequest(method, v.address+path, body)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := v.client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer response.Body.Close()
	content, err := io.ReadAll(io.LimitReader(response.Body, 16*1024*1024+1))
	if len(content) > 16*1024*1024 {
		return response.StatusCode, nil, fmt.Errorf("response capacity exceeded")
	}
	return response.StatusCode, content, err
}
func bytesReader(data []byte) io.Reader { return bytes.NewReader(data) }
func (v *verifier) publish(s snapshot, want int) error {
	code, data, err := v.request("POST", "/snapshot", v.token, s)
	if err != nil {
		return err
	}
	if code != want {
		return fmt.Errorf("publish status %d, want %d: %s", code, want, data)
	}
	return nil
}
func (v *verifier) current() (view, error) {
	code, data, err := v.request("GET", "/view", "", nil)
	if err != nil {
		return view{}, err
	}
	if code != 200 {
		return view{}, fmt.Errorf("view status %d", code)
	}
	var result view
	err = json.Unmarshal(data, &result)
	return result, err
}
func (v *verifier) check(name string, run func() error) {
	start := time.Now()
	err := run()
	o := observation{Case: name, Passed: err == nil, ElapsedMS: time.Since(start).Milliseconds()}
	if err != nil {
		o.Error = err.Error()
	}
	v.observations = append(v.observations, o)
	fmt.Fprintf(os.Stderr, "%s: %t %s\n", name, o.Passed, o.Error)
}
func main() {
	binary := flag.String("router", "target/debug/edgelab-routing", "router executable")
	output := flag.String("output", ".run/routing-foundation", "evidence directory")
	flag.Parse()
	if err := run(*binary, *output); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func run(binary, output string) error {
	if err := os.MkdirAll(output, 0700); err != nil {
		return err
	}
	dir, err := os.MkdirTemp(output, "run-")
	if err != nil {
		return err
	}
	binary, err = filepath.Abs(binary)
	if err != nil {
		return err
	}
	socket, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	address := socket.Addr().String()
	socket.Close()
	token := "fixture-only-routing-verifier-token-0001"
	config := map[string]any{"owners": map[string]any{"worker-1": map[string]any{"token": token, "applications": []string{"echo"}}, "worker-2": map[string]any{"token": token + "2", "applications": []string{"echo"}}}}
	data, _ := json.Marshal(config)
	if err = os.WriteFile(filepath.Join(dir, "config.json"), data, 0600); err != nil {
		return err
	}
	v := verifier{client: &http.Client{Timeout: 4 * time.Second}, address: "http://" + address, token: token}
	var process *exec.Cmd
	var log *os.File
	stop := func() {
		if process != nil {
			process.Process.Kill()
			process.Wait()
			process = nil
		}
		if log != nil {
			log.Close()
			log = nil
		}
	}
	defer stop()
	start := func() error {
		log, err = os.OpenFile(filepath.Join(dir, "router.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
		if err != nil {
			return err
		}
		process = exec.Command(binary, "--config", filepath.Join(dir, "config.json"), "--state", filepath.Join(dir, "state.json"), "--listen", address)
		process.Stdout = log
		process.Stderr = log
		if err = process.Start(); err != nil {
			return err
		}
		deadline := time.Now().Add(4 * time.Second)
		for time.Now().Before(deadline) {
			if code, _, e := v.request("GET", "/health", "", nil); e == nil && code == 200 {
				return nil
			}
			time.Sleep(20 * time.Millisecond)
		}
		return fmt.Errorf("router readiness timeout")
	}
	if err = start(); err != nil {
		return err
	}
	initial := snapshot{Schema: 1, Owner: "worker-1", Incarnation: 1, Revision: 1, Records: map[string]record{"one": {ID: "one", App: "echo", Endpoint: "127.0.0.1:19001", Revision: 1}}}
	v.check("authenticated-publication", func() error { return v.publish(initial, 200) })
	v.check("wrong-credential", func() error {
		code, _, err := v.request("POST", "/snapshot", "wrong", initial)
		if err != nil {
			return err
		}
		if code != 401 {
			return fmt.Errorf("got %d", code)
		}
		return nil
	})
	v.check("conflicting-revision-retains-active-view", func() error {
		changed := snapshot{Schema: 1, Owner: "worker-1", Incarnation: 1, Revision: 1, Records: map[string]record{"one": {ID: "one", App: "echo", Endpoint: "127.0.0.1:19002", Revision: 1}}}
		if err := v.publish(changed, 409); err != nil {
			return err
		}
		current, err := v.current()
		if err != nil {
			return err
		}
		if current.Owners["worker-1"].Snapshot.Records["one"].Endpoint != "127.0.0.1:19001" {
			return fmt.Errorf("rejected endpoint activated")
		}
		return nil
	})
	deleted := snapshot{Schema: 1, Owner: "worker-1", Incarnation: 1, Revision: 2, Records: map[string]record{"one": {ID: "one", App: "echo", Endpoint: "127.0.0.1:19001", Revision: 2, Deleted: true}}}
	v.check("deletion-rejects-stale-replay", func() error {
		if err := v.publish(deleted, 200); err != nil {
			return err
		}
		return v.publish(initial, 409)
	})
	v.check("new-incarnation-cannot-resurrect-backup", func() error { restored := initial; restored.Incarnation = 2; return v.publish(restored, 409) })
	v.check("retired-endpoint-reserved-across-owners", func() error {
		reused := initial
		reused.Owner = "worker-2"
		code, _, err := v.request("POST", "/snapshot", token+"2", reused)
		if err != nil {
			return err
		}
		if code != 409 {
			return fmt.Errorf("got %d", code)
		}
		return nil
	})
	before, err := v.current()
	if err != nil {
		return err
	}
	v.check("delivery-does-not-refresh-source", func() error {
		time.Sleep(30 * time.Millisecond)
		after, err := v.current()
		if err != nil {
			return err
		}
		if after.Owners["worker-1"].Reconciled != before.Owners["worker-1"].Reconciled {
			return fmt.Errorf("source freshness changed on delivery")
		}
		return nil
	})
	v.check("process-restart-retains-deletion-and-marks-stale", func() error {
		stop()
		if err := start(); err != nil {
			return err
		}
		current, err := v.current()
		if err != nil {
			return err
		}
		owner := current.Owners["worker-1"]
		if !owner.Stale || !owner.Snapshot.Records["one"].Deleted || owner.Reconciled != before.Owners["worker-1"].Reconciled {
			return fmt.Errorf("restart lost deletion or source freshness")
		}
		return nil
	})
	v.check("record-capacity-preserves-deletion", func() error {
		large := snapshot{Schema: 1, Owner: "worker-1", Incarnation: 1, Revision: 3, Records: map[string]record{"one": deleted.Records["one"]}}
		for i := 1; i < 4096; i++ {
			id := fmt.Sprintf("instance-%04d", i)
			large.Records[id] = record{ID: id, App: "echo", Endpoint: fmt.Sprintf("127.0.0.1:%d", 20000+i), Revision: 3}
		}
		if err := v.publish(large, 200); err != nil {
			return err
		}
		large.Revision = 4
		large.Records["excess"] = record{ID: "excess", App: "echo", Endpoint: "127.0.0.1:30000", Revision: 4}
		if err := v.publish(large, 409); err != nil {
			return err
		}
		delete(large.Records, "excess")
		r := large.Records["instance-0001"]
		r.Revision = 4
		r.Deleted = true
		large.Records[r.ID] = r
		if err := v.publish(large, 200); err != nil {
			return err
		}
		current, err := v.current()
		if err != nil {
			return err
		}
		if len(current.Owners["worker-1"].Snapshot.Records) != 4096 || !current.Owners["worker-1"].Snapshot.Records[r.ID].Deleted {
			return fmt.Errorf("capacity deletion missing")
		}
		return nil
	})
	stop()
	failed := 0
	for _, o := range v.observations {
		if !o.Passed {
			failed++
		}
	}
	result := map[string]any{"scope": "routing foundation; not full Release 4 acceptance", "observations": v.observations, "failed": failed, "interruption": "process kill only"}
	if revision, err := os.ReadFile(".source-revision"); err == nil {
		result["source_commit"] = string(bytes.TrimSpace(revision))
	}
	evidence, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return err
	}
	if err = os.WriteFile(filepath.Join(dir, "results.json"), append(evidence, '\n'), 0600); err != nil {
		return err
	}
	fmt.Println(filepath.Join(dir, "results.json"))
	if failed > 0 {
		return fmt.Errorf("%d checks failed", failed)
	}
	return nil
}
