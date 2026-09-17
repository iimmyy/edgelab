import pathlib,subprocess,socket,json,time,hashlib
root=pathlib.Path("/home/ubuntu/edgelab");out=root/".run/handshake-light";children=[];files=[]
def start(args,name):
 f=(out/name).open("w");files.append(f);p=subprocess.Popen(args,stdout=f,stderr=f,cwd=root);children.append(p);return p
def port():
 s=socket.socket();s.bind(("127.0.0.1",0));v=s.getsockname()[1];s.close();return v
def ready(v):
 for _ in range(100):
  try:
   with socket.create_connection(("127.0.0.1",v),.1):return
  except OSError:time.sleep(.05)
 raise RuntimeError("listener notready")
def attached(path,marker,proc):
 for _ in range(150):
  if marker in path.read_text():return
  if proc.poll() is not None:raise RuntimeError(path.read_text())
  time.sleep(.1)
 raise RuntimeError("attachment notconfirmed:"+str(path))
def snap(name):
 d={f:pathlib.Path(f).read_text() for f in ["/proc/net/netstat","/proc/net/snmp","/proc/net/sockstat","/proc/pressure/cpu"]};d["clock"]={"time_ns":time.time_ns(),"monotonic_ns":time.monotonic_ns()};(out/name).write_text(json.dumps(d,indent=2))
backend=port();front=port()
capturefilter=f"ip and tcp port {backend} and ((tcp[tcpflags] & (tcp-syn|tcp-fin|tcp-rst) != 0) or ((ip[2:2] - ((ip[0] & 15) << 2) - ((tcp[12] & 240) >> 2)) = 0))"
(out/"config.json").write_text(json.dumps({"Apps":[{"Name":"diag","Ports":[front],"Targets":[f"127.0.0.1:{backend}"]}]}))
(out/"identity.json").write_text(json.dumps({"backend":backend,"front":front,"binary_sha256":hashlib.sha256((root/"target/release/edgelab-proxy").read_bytes()).hexdigest(),"target_ms":500,"concurrency":100,"payload_bytes":1024,"capture_ring_files":24,"capture_file_million_bytes":128,"capture_snaplen":96,"capture_filter":capturefilter,"probe_count":2},indent=2))
(out/"trace.bt").write_text("\ntracepoint:skb:kfree_skb {\n $skb=(struct sk_buff *)args->skbaddr; $h=(uint8 *)($skb->head+$skb->transport_header);\n if (($h[13]&2) && ((($h[2]<<8)|$h[3])==BACKEND)) {\n printf(\"syn_drop ns=%llu reason=%d location=%s sport=%d dport=%d seq=%u\\n\",nsecs,args->reason,ksym(args->location),($h[0]<<8)|$h[1],($h[2]<<8)|$h[3],($h[4]<<24)|($h[5]<<16)|($h[6]<<8)|$h[7]);\n }\n}\nkprobe:tcp_send_challenge_ack {\n $sk=(struct sock *)arg0; $tp=(struct tcp_sock *)arg0; $dp=$sk->__sk_common.skc_dport;\n if($sk->__sk_common.skc_num==BACKEND) {\n printf(\"challenge ns=%llu state=%d sport=%d dport=%d rcv_nxt=%u snd_nxt=%u snd_una=%u\\n\",nsecs,$sk->__sk_common.skc_state,$sk->__sk_common.skc_num,(($dp&255)<<8)|($dp>>8),$tp->rcv_nxt,$tp->snd_nxt,$tp->snd_una);\n }\n}\n".replace("BACKEND",str(backend)))
traceproc=capture=None
try:
 echo=start([str(root/"bin/echo"),"--listen",f"127.0.0.1:{backend}","--app","diag","--instance","diag1"],"echo.log");ready(backend)
 proxy=start([str(root/"target/release/edgelab-proxy"),"--config",str(out/"config.json")],"proxy.log");ready(front)
 traceproc=start(["sudo","sh","-c",f"echo $$ > {out}/trace.pid; exec bpftrace -B line {out}/trace.bt"],"trace.log");attached(out/"trace.log","Attaching 2 probes",traceproc)
 capture=start(["sudo","sh","-c",f"echo $$ > {out}/capture.pid; exec tcpdump -Z root -i lo -nn -s96 -B16384 -C128 -W24 -w {out}/tcp.pcap '{capturefilter}'"],"capture.log");attached(out/"capture.log","listening on lo",capture)
 time.sleep(.5);snap("kernel-initial.json");print("READY2probes+controlcapture; backend="+str(backend),flush=True)
 done=False
 for pair in range(1,4):
  for route,dest in [("direct",backend),("proxy",front)]:
   label=f"{pair}-{route}";snap(f"kernel-before-{label}.json")
   with (out/f"traffic-{label}.json").open("w") as f:
    r=subprocess.run([str(root/"bin/traffic"),"--address",f"127.0.0.1:{dest}","--app","diag","--instances","diag1","--count","4000000","--concurrency","100","--bytes","1024","--duration","60s","--history",str(out/f"traffic-{label}.jsonl")],stdout=f,stderr=subprocess.PIPE,text=True,cwd=root)
   snap(f"kernel-after-{label}.json");report=json.loads((out/f"traffic-{label}.json").read_text());print(json.dumps({"pair":pair,"route":route,"report":report,"returncode":r.returncode}),flush=True)
   if route=="proxy" and report["failures"]:done=True;break
  if done:break
finally:
 for name,proc in [("capture",capture),("trace",traceproc)]:
  if proc is not None and proc.poll() is None:
   subprocess.run(["sudo","kill","-INT",(out/f"{name}.pid").read_text().strip()])
   try:proc.wait(timeout=8)
   except subprocess.TimeoutExpired:pass
 for p in reversed(children):
  if p.poll() is None:
   p.terminate()
   try:p.wait(timeout=7)
   except subprocess.TimeoutExpired:p.kill();p.wait()
 for f in files:f.close()
 snap("kernel-final.json");print("STOPPED "+str(out),flush=True)
