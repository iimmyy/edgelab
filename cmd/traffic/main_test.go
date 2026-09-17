package main

import (
	"encoding/json"
	"io"
	"net"
	"strings"
	"testing"
	"time"
)

func TestIndependentIdentityRejectsWrongWorker(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			return
		}
		defer connection.Close()
		connection.SetDeadline(time.Now().Add(time.Second))
		json.NewEncoder(connection).Encode(map[string]string{"instance": "one", "app": "echo", "worker": "wrong-worker", "region": "west", "version": "v1"})
		io.Copy(connection, connection)
	}()
	expected := map[string]map[string]string{"one": {"app": "echo", "worker": "worker-1", "region": "west", "version": "v1"}}
	result := request(listener.Addr().String(), "echo", map[string]bool{"one": true}, expected, 10, time.Second, 1)
	if !strings.Contains(result.Error, "worker mismatch") {
		t.Fatalf("wrong worker passed: %+v", result)
	}
}
