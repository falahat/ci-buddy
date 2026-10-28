"""Install the reviewed runner on an existing tailworker machine, then route work."""
import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib

import auth
import ssh


REMOTE = r'''import fcntl,io,json,os,pathlib,shlex,subprocess,sys,tarfile,tempfile,tomllib
os.umask(0o077)
data=sys.stdin.buffer.read(); size=int.from_bytes(data[:8],'big')
assert 0<size<=65536
request=json.loads(data[8:8+size]); revision=request['revision']
root=pathlib.Path(sys.argv[1]).expanduser().absolute()
if root.exists():
 assert root.is_dir() and root.stat().st_uid==os.getuid() and root.stat().st_mode&0o077==0
else:
 assert request['mode']=='install'
 root.mkdir(mode=0o700,parents=True)
root=root.resolve()
def identity():
 return [(pathlib.Path(os.sep)/'proc'/'sys'/'kernel'/'random'/'boot_id').read_text().strip(),root.stat().st_dev,root.stat().st_ino]
def write(value):
 with tempfile.NamedTemporaryFile(dir=root,delete=False) as output:
  output.write(value); temporary=pathlib.Path(output.name)
 temporary.replace(root/'host.conf')
with (root/'enrollment.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 conf=root/'host.conf'; old=conf.read_bytes() if conf.exists() else None
 if old is not None:
  assert (root/'bin/revision').read_text().strip()==revision, 'update the existing runner explicitly'
  if request['mode']=='install': assert tomllib.loads(old.decode())==tomllib.loads(request['conf']), 'existing host.conf differs'
 else:
  assert request['mode']=='install' and not any(item.name!='enrollment.lock' for item in root.iterdir()), 'runner root is not empty'
  write(request['conf'].encode())
 if request['mode']=='register':
  assert identity()!=request['identity'], 'a runner cannot dispatch to itself'
  host,peer=request['endpoint'].split(':',1)
  probe=['bash',str(pathlib.Path(peer)/'bin/ci-buddy'),'--server','enrollment','version']
  if host=='local':
   assert identity()[0]==request['identity'][0], 'local worker belongs to another control host'
   target=pathlib.Path(peer).stat()
   assert [target.st_dev,target.st_ino]==request['identity'][1:], 'local worker root differs'
  else: probe=['python3',str(root/'bin/ssh.py'),host,shlex.join(probe)]
  assert subprocess.check_output(probe,text=True).strip()==revision, 'coordinator cannot reach the reviewed worker'
  limits=tomllib.loads(old.decode())
  if request['endpoint'] in limits['workers']:
   print(json.dumps(identity())); sys.exit()
  limits['workers'].append(request['endpoint'])
  write(('\n'.join(key+' = '+json.dumps(value) for key,value in limits.items())+'\n').encode())
 stage=root/('enroll-'+revision)
 try:
  if old is None or request['mode']=='register':
   stage.mkdir(mode=0o700,exist_ok=True)
   with tarfile.open(fileobj=io.BytesIO(data[8+size:])) as archive:
    assert all(member.isfile() or member.isdir() for member in archive), 'runner archive contains links or special files'
    archive.extractall(stage,filter='data')
   (stage/'revision').write_text(revision+'\n')
   with (root/'enrollment.log').open('wb') as log:
    subprocess.run(['bash',str(stage/'install.sh'),'--root',str(root)],check=True,stdout=log,stderr=subprocess.STDOUT)
  result=subprocess.check_output(['bash',str(root/'bin/ci-buddy'),'--server','enrollment','version'],text=True)
  assert result.strip()==revision, 'installed runner revision or readiness differs'
 except BaseException:
  if request['mode']=='register':
   write(old)
   previous=tomllib.loads(old.decode())
   subprocess.run([str(root/'bin/pueue'),'--config',str(root/'pueue.yml'),'parallel','--group','jobs',
    str(previous['jobs']*(1+len(previous['workers'])))],check=True,stdout=subprocess.DEVNULL)
  raise
print(json.dumps(identity()))
'''


def runtime(source):
    if (source / 'revision').is_file():
        revision = (source / 'revision').read_text().strip()
        archive = (source / 'runtime.tar').read_bytes()
    else:
        subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--'], check=True)
        revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip()
        archive = subprocess.check_output(['git', '-C', str(source), 'archive', '--format=tar', 'HEAD'])
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('invalid reviewed runner revision')
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        if any(not member.isfile() and not member.isdir() for member in contents):
            raise ValueError('reviewed runner archive contains a link or special file')
    return revision, archive


def invoke(endpoint, request, archive):
    host, separator, root = endpoint.partition(':')
    root = root if separator else '.ci-buddy'
    if (not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host)
            or not (root == '.ci-buddy' or root.startswith('/')) or any(c in root for c in '\r\n')):
        raise ValueError('invalid approved runner endpoint')
    if host == 'local' and root == '.ci-buddy':
        root = str(Path.home() / root)
    command = ['python3', '-I', '-c', REMOTE, root]
    argv = [sys.executable, '-I', '-c', REMOTE, root] if host == 'local' else ssh.command(host, shlex.join(command))
    header = json.dumps(request).encode()
    with tempfile.TemporaryFile() as payload:
        payload.write(len(header).to_bytes(8, 'big') + header + archive)
        payload.seek(0)
        return json.loads(ssh.capture(argv, 4096, payload=payload))


def enroll(args):
    conf = Path(args.host_conf).read_text()
    if len(conf.encode()) > 32768 or tomllib.loads(conf)['workers'] != []:
        raise ValueError('provide an explicit leaf host.conf with workers = []')
    root = PurePosixPath(args.root)
    if not root.is_absolute() or str(root) != args.root or '..' in root.parts or any(c in args.root for c in '\r\n'):
        raise ValueError('provide a normalized absolute runner root')
    source = Path(__file__).resolve().parent
    os.chdir(source)
    # Absolute owner PATH entries only; Windows must not search submitted cwd.
    for entry in os.get_exec_path():
        candidate = Path(entry) / 'tailworker'
        if Path(entry).is_absolute() and candidate.is_file():
            machine = json.loads(ssh.capture([sys.executable, str(candidate.resolve()), 'resolve', args.name], 16384))
            break
    else:
        raise ValueError('install the reviewed tailworker command on the owner PATH')
    if machine['name'] != args.name or machine['kind'] not in ('owned', 'dedicated') or machine['online'] is not True:
        raise ValueError('tailworker machine is not an existing online Linux runner')
    host = machine['host']
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host) or (host == 'local' and os.name != 'posix'):
        raise ValueError('local runners need Linux/WSL; remote machines need their pinned SSH alias')
    configured = subprocess.check_output(['git', 'config', '--global', '--get-all', 'ci-buddy.host']).decode().splitlines()
    coordinator = args.coordinator or configured[0]
    if coordinator not in configured:
        raise ValueError('coordinator is outside the owner-approved routing pool')
    revision, archive = runtime(source)
    endpoint = host + ':' + str(root)
    identity = invoke(endpoint, dict(mode='install', revision=revision, conf=conf), archive)
    added = endpoint not in configured
    if added:
        subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', endpoint], check=True)
    try:
        auth.client([endpoint, *configured])
        invoke(coordinator, dict(mode='register', revision=revision, endpoint=endpoint, identity=identity), archive)
    except BaseException:
        if added:
            subprocess.run(['git', 'config', '--global', '--fixed-value', '--unset-all', 'ci-buddy.host', endpoint], check=True)
        raise
    print('enrolled=' + args.name + ' endpoint=' + endpoint + ' coordinator=' + coordinator)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='existing logical tailworker machine name')
    parser.add_argument('--root', required=True)
    parser.add_argument('--host-conf', required=True)
    parser.add_argument('--coordinator', help='existing endpoint in owner ci-buddy.host configuration')
    try:
        enroll(parser.parse_args())
    except (OSError, ValueError, KeyError, IndexError, subprocess.CalledProcessError) as error:
        detail = 'native command failed' if isinstance(error, subprocess.CalledProcessError) else str(error)
        raise SystemExit('ci-buddy enrollment failed; inspect the dedicated root and enrollment.log: ' + detail) from None
