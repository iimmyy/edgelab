package main

import (
	"encoding/json"
	"flag"
	"io"
	"log"
	"net"
	"time"
)

func main() {
	address := flag.String("listen", "127.0.0.1:9101", "listen address")
	app := flag.String("app", "echo", "application identity")
	instance := flag.String("instance", "echo-a", "instance identity")
	worker := flag.String("worker", "worker-a", "logical worker identity")
	region := flag.String("region", "local-a", "logical region")
	version := flag.String("version", "v1", "deployed version")
	flag.Parse()
	listener, err := net.Listen("tcp", *address)
	if err != nil {
		log.Fatal(err)
	}
	identity, _ := json.Marshal(map[string]string{"app": *app, "instance": *instance, "worker": *worker, "region": *region, "version": *version})
	identity = append(identity, '\n')
	slots := make(chan struct{}, 512)
	for {
		conn, err := listener.Accept()
		if err != nil {
			log.Fatal(err)
		}
		select {
		case slots <- struct{}{}:
		default:
			conn.Close()
			continue
		}
		go func() {
			defer func() { conn.Close(); <-slots }()
			conn.SetDeadline(time.Now().Add(2 * time.Minute))
			if _, err := conn.Write(identity); err != nil {
				return
			}
			if _, err := io.Copy(conn, conn); err == nil {
				io.WriteString(conn, "\nEOF\n")
			}
		}()
	}
}
