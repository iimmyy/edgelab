#!/usr/bin/env python3
"""Owned, disposable Linux fixtures for Release 2."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path('/var/lib/edgelab-r2')
PROJECT = Path(__file__).resolve().parent.parent
CLIENT, SERVER = 'el-client', 'el-server'


def run(*args, check=True, input=None):
    p = subprocess.run(list(map(str, args)), input=input, text=True, capture_output=True, timeout=30)
    if check and p.returncode:
        raise RuntimeError(f'{args[0]}: {p.stderr.strip()} {p.stdout.strip()}')
    return p.stdout.strip()


def ns(name, *args, **kw):
    return run('ip', 'netns', 'exec', name, *args, **kw)


def save(name, value):
    path = ROOT / name
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    tmp.replace(path)
    fd = os.open(ROOT, os.O_DIRECTORY); os.fsync(fd); os.close(fd)


def machine():
    return Path('/etc/machine-id').read_text().strip()


def guard():
    if os.geteuid() != 0 or sys.platform != 'linux':
        raise RuntimeError('requires root in the authorized Linux VM')
    if ROOT.is_symlink() or not (ROOT / 'marker.json').is_file():
        raise RuntimeError('unmarked environment')
    st = ROOT.stat()
    if st.st_uid != 0 or st.st_mode & 0o077:
        raise RuntimeError('fixture directory must be root owned and mode 0700')
    marker = json.loads((ROOT / 'marker.json').read_text())
    if marker['machine'] != machine():
        raise RuntimeError('machine identity mismatch')


def preflight():
    if os.geteuid() != 0 or sys.platform != 'linux':
        raise RuntimeError('requires root on Linux')
    for tool in ['ip','wg','nft','losetup','pvcreate','vgcreate','lvcreate','mkfs.ext4','resize2fs','tcpdump']:
        if not shutil.which(tool): raise RuntimeError(f'missing {tool}')
    if run('systemd-detect-virt') not in ('qemu', 'kvm'):
        raise RuntimeError('fixture requires a disposable virtual machine')
    return {'machine':machine(), 'kernel':os.uname().release, 'architecture':os.uname().machine,
            'free_bytes':shutil.disk_usage('/var/lib').free,
            'devices':json.loads(run('lsblk','--json','-b','-o','NAME,SIZE,TYPE,MOUNTPOINTS'))}


def init():
    facts = preflight()
    if ROOT.exists(): guard(); return facts
    if facts['free_bytes'] < 12 * 1024**3: raise RuntimeError('less than 12 GiB free')
    names = run('ip','netns','list')
    if any(n in names for n in (CLIENT, SERVER)) or run('vgs','edgelab_r2',check=False):
        raise RuntimeError('reserved resource name already exists')
    ROOT.mkdir(mode=0o700)
    save('marker.json',facts)
    return facts


def storage():
    path = ROOT / 'storage.json'
    if path.exists():
        f = json.loads(path.read_text()); validate_storage(f)
        run('lvchange','--devices',','.join(f['devices']),'-ay',f['lv'])
        if not os.path.ismount(f['mount']): run('mount',f['lv'],f['mount'])
        return f
    if (ROOT / 'storage-started').exists():
        raise RuntimeError('interrupted initial provisioning; preserve resources for inspection')
    (ROOT / 'storage-started').touch()
    devices=[]
    for i in range(4):
        p=ROOT/f'disk{i}'; run('fallocate','-l',1800*1024**2,p)
        devices.append(run('losetup','--find','--show',p))
        save('devices.json',devices)
    allowed=','.join(devices)
    run('pvcreate','--devices',allowed,*devices)
    run('vgcreate','--devices',allowed,'edgelab_r2',*devices)
    run('lvcreate','--devices',allowed,'-L','1024M','-n','data','edgelab_r2')
    lv='/dev/edgelab_r2/data'; run('mkfs.ext4','-q',lv)
    mount=ROOT/'volume'; mount.mkdir(); run('mount',lv,mount)
    with (mount/'.owner').open('w') as f: f.write('objects-1\n'); f.flush(); os.fsync(f.fileno())
    fd=os.open(mount,os.O_DIRECTORY);os.fsync(fd);os.close(fd)
    result={'machine':machine(),'devices':devices,'lv':lv,'mount':str(mount),
            'uuid':run('lvs','--devices',allowed,'--noheadings','-o','lv_uuid',lv),
            'filesystem_uuid':run('blkid','-s','UUID','-o','value',lv)}
    save('storage.json',result); return result


def validate_storage(f):
    if f['machine'] != machine() or len(f['devices'])!=4: raise RuntimeError('invalid storage ownership')
    for i,d in enumerate(f['devices']):
        if run('losetup','-n','-O','BACK-FILE',d)!=str(ROOT/f'disk{i}'):
            raise RuntimeError('loop device outside owned allowlist')
    if run('lvs','--devices',','.join(f['devices']),'--noheadings','-o','lv_uuid',f['lv']) != f['uuid']:
        raise RuntimeError('LV identity mismatch')


def network():
    existing=run('ip','netns','list')
    if CLIENT in existing and SERVER in existing:
        if not (ROOT/'network-ready').exists(): baseline(); (ROOT/'network-ready').touch()
        return
    if CLIENT in existing or SERVER in existing: raise RuntimeError('partial network; inspect before repair')
    for name in (CLIENT,SERVER): run('ip','netns','add',name); ns(name,'ip','link','set','lo','up')
    run('ip','link','add','el-c','type','veth','peer','name','el-s')
    run('ip','link','set','el-c','netns',CLIENT);run('ip','link','set','el-s','netns',SERVER)
    for name,link,address in [(CLIENT,'el-c','172.30.77.1/30'),(SERVER,'el-s','172.30.77.2/30')]:
        ns(name,'ip','addr','add',address,'dev',link);ns(name,'ip','link','set',link,'up')
        key=ROOT/(name+'.key');key.write_text(run('wg','genkey')+'\n');key.chmod(0o600)
        ns(name,'ip','link','add','wg0','type','wireguard')
        ns(name,'wg','set','wg0','private-key',key,'listen-port','51820')
    baseline()
    (ROOT/'network-ready').touch()


def public(name): return run('wg','pubkey',input=(ROOT/(name+'.key')).read_text())


def baseline():
    for name,address,peer,endpoint in [(CLIENT,'10.77.0.1',SERVER,'172.30.77.2'),(SERVER,'10.77.0.2',CLIENT,'172.30.77.1')]:
        ns(name,'ip','addr','flush','dev','wg0');ns(name,'ip','addr','add',address+'/32','dev','wg0')
        target='10.77.0.2' if name==CLIENT else '10.77.0.1'
        ns(name,'wg','set','wg0','peer',public(peer),'allowed-ips',target+'/32','endpoint',endpoint+':51820')
        ns(name,'ip','link','set','wg0','mtu','1420','up')
        ns(name,'ip','route','replace',target+'/32','dev','wg0')
    ns(SERVER,'nft','delete','table','inet','edgelab',check=False)
    ns(SERVER,'nft','-f','-',input='table inet edgelab { chain input { type filter hook input priority 0; policy accept; iifname "el-s" tcp dport { 8101, 9000 } counter drop; }; }\n')


def alive(name):
    p=ROOT/(name+'.pid')
    if not p.exists(): return False
    record=json.loads(p.read_text()); proc=Path('/proc')/str(record['pid'])
    try: return proc.joinpath('stat').read_text().split()[21]==record['start']
    except FileNotFoundError:return False


def start(name,args,namespace=SERVER):
    if alive(name): return
    with (ROOT/(name+'.log')).open('ab') as log:
        p=subprocess.Popen(['ip','netns','exec',namespace,*map(str,args)],stdout=log,stderr=log,start_new_session=True)
    save(name+'.pid',{'pid':p.pid,'start':Path(f'/proc/{p.pid}/stat').read_text().split()[21]})
    time.sleep(.2)
    if p.poll() is not None: raise RuntimeError(f'{name} failed: {(ROOT/(name+".log")).read_text()[-2000:]}')


def stop(name):
    if alive(name):
        pid=json.loads((ROOT/(name+'.pid')).read_text())['pid'];os.kill(pid,15)
        for _ in range(60):
            if not alive(name): break
            time.sleep(.1)
        if alive(name):os.kill(pid,9)


def workloads():
    start('objects',[PROJECT/'bin/objects','--root',ROOT/'volume','--owner','objects-1','--listen','0.0.0.0:9000','--management','0.0.0.0:9001'])
    save('proxy.json',{'Apps':[{'Name':'objects','Ports':[8101],'Targets':['127.0.0.1:9000']}]})
    start('proxy',[PROJECT/'target/release/edgelab-proxy','--config',ROOT/'proxy.json','--listen-ip','0.0.0.0'])


def fault(name):
    if name=='interface': ns(CLIENT,'ip','link','set','wg0','down')
    elif name=='address': ns(CLIENT,'ip','addr','del','10.77.0.1/32','dev','wg0');ns(CLIENT,'ip','addr','add','10.77.0.9/32','dev','wg0');ns(CLIENT,'ip','route','replace','10.77.0.2/32','dev','wg0')
    elif name=='endpoint':ns(CLIENT,'wg','set','wg0','peer',public(SERVER),'endpoint','172.30.77.2:51821')
    elif name=='prefix':ns(CLIENT,'wg','set','wg0','peer',public(SERVER),'allowed-ips','10.77.0.99/32')
    elif name=='route':ns(CLIENT,'ip','route','replace','blackhole','10.77.0.2/32')
    elif name=='firewall':ns(SERVER,'nft','add','rule','inet','edgelab','input','udp','dport','51820','counter','drop')
    elif name=='mtu':ns(SERVER,'nft','add','rule','inet','edgelab','input','udp','dport','51820','meta','length','>','1280','counter','drop')
    else:raise RuntimeError('unknown fault')


def status():
    result={'time':time.time(),'machine':machine(),'namespaces':run('ip','netns','list'),'processes':{n:json.loads((ROOT/(n+'.pid')).read_text()) for n in ['objects','proxy'] if (ROOT/(n+'.pid')).exists()}}
    for n in [CLIENT,SERVER]:result[n]={'addresses':ns(n,'ip','-j','addr'),'routes':ns(n,'ip','-j','route'),'wireguard':ns(n,'wg','show'),'rules':ns(n,'nft','list','ruleset')}
    if (ROOT/'storage.json').exists():
        f=json.loads((ROOT/'storage.json').read_text());result['storage']=f
        result['lvs']=run('lvs','--devices',','.join(f['devices']),'--reportformat','json','--units','b','-o','lv_name,lv_size,lv_uuid','edgelab_r2')
        result['physical_bytes']=[(ROOT/f'disk{i}').stat().st_blocks*512 for i in range(4)]
    return result


def down():
    for n in ['proxy','objects','empty']:stop(n)
    f=json.loads((ROOT/'storage.json').read_text());validate_storage(f)
    if os.path.ismount(f['mount']):run('umount',f['mount'])
    run('vgremove','--devices',','.join(f['devices']),'-ff','-y','edgelab_r2')
    for d in f['devices']:run('losetup','-d',d)
    for n in [CLIENT,SERVER]:run('ip','netns','del',n)
    shutil.rmtree(ROOT)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['init','up','down','status','baseline','fault','repair-mtu','stop-owner','start-owner','recover-storage']);p.add_argument('--name');p.add_argument('--confirm-disposable',action='store_true');a=p.parse_args()
    try:
        if a.action=='init':
            if not a.confirm_disposable:raise RuntimeError('explicit disposable VM flag required')
            print(json.dumps(init(),indent=2))
        else:
            guard()
            if a.action=='up':storage();network();workloads();print(json.dumps(status(),indent=2))
            elif a.action=='down':down()
            elif a.action=='status':print(json.dumps(status(),indent=2))
            elif a.action=='baseline':baseline()
            elif a.action=='fault':fault(a.name)
            elif a.action=='repair-mtu':
                for n in [CLIENT,SERVER]:ns(n,'ip','link','set','wg0','mtu','1200')
            elif a.action=='stop-owner':stop('objects')
            elif a.action=='start-owner':workloads()
            elif a.action=='recover-storage':storage();workloads()
    except Exception as e:print(json.dumps({'ok':False,'error':str(e)}),file=sys.stderr);sys.exit(1)
