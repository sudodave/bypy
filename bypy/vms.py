#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2019, Kovid Goyal <kovid at kovidgoyal.net>

import atexit
import json
import os
import shlex
import subprocess
from functools import lru_cache
from time import monotonic, sleep

from .conf import parse_conf_file
from .constants import base_dir
from virtual_machine.utils import read_build_server

ssh_masters = set()
BUILD_SERVER_USER, BUILD_SERVER, BUILD_SERVER_VM_CD = read_build_server()
VM_SERVER = f'kovid@{BUILD_SERVER}'
BUILD_SERVER_WITH_USER = BUILD_SERVER
if BUILD_SERVER_USER:
    BUILD_SERVER_WITH_USER = f'{BUILD_SERVER_USER}@{BUILD_SERVER}'


def end_ssh_master(address, socket, process):
    server, port = address
    subprocess.run(['ssh', '-O', 'exit', '-S', socket, '-p', port, server])
    if process.poll() is None:
        process.terminate()
    if process.poll() is None:
        sleep(0.1)
        process.kill()
    ssh_masters.discard(address)


def ssh_to(
    port=22, server=BUILD_SERVER, user=BUILD_SERVER_USER
):
    if user:
        server = f'{user}@{server}'
    socket = os.path.expanduser(
        f'~/.ssh/controlmasters/bypy-{server}-{port}')
    os.makedirs(os.path.dirname(socket), exist_ok=True)
    port = str(port)
    address = server, port
    ssh = ['ssh', '-p', port, '-S', socket]
    if address not in ssh_masters:
        ssh_masters.add(address)
        atexit.register(
            end_ssh_master, address, socket,
            subprocess.Popen(ssh + ['-M', '-N', server]))
    return ssh


def ssh_to_vm(name):
    m = vm_metadata(name)
    port = m['ssh_port']
    return ssh_to(port=int(port), server=VM_SERVER, user=None)


def get_rsync_conf():
    ans = getattr(get_rsync_conf, 'ans', None)
    if ans is None:
        ans = get_rsync_conf.ans = parse_conf_file(
                os.path.join(base_dir(), 'rsync.conf'))
    return ans


def wait_for_ssh(name):
    st = monotonic()
    print('Waiting for SSH server to start...', flush=True)
    m = vm_metadata(name)
    port = m['ssh_port']
    cmd = ['ssh', '-p', str(port), VM_SERVER, 'date']
    while True:
        cp = subprocess.run(cmd)
        if cp.returncode == 0:
            break
        sleep(0.2)
    print(
        'SSH server started in {:.1f} seconds'.format(monotonic() - st),
        flush=True)


def vm_cmd(name, *args, get_output=False):
    if len(args) == 1:
        args = shlex.split(args[0])
    cmd = ssh_to()
    cmd += [BUILD_SERVER_WITH_USER] + list(BUILD_SERVER_VM_CD) + list(args)
    kw = {}
    if get_output:
        kw['stdout'] = subprocess.PIPE
    p = subprocess.run(cmd, **kw)
    if p.returncode != 0:
        q = shlex.join(args)
        raise SystemExit(
            f'The command: {q} failed with error code: {p.returncode}')
    return p.stdout


@lru_cache(maxsize=2)
def vm_metadata(name):
    return json.loads(vm_cmd(name, 'status', name, get_output=True))


def run_in_vm(name, *args, **kw):
    if len(args) == 1:
        args = shlex.split(args[0])
    p = subprocess.Popen(ssh_to_vm(name) + ['-t', VM_SERVER] + list(args))
    if kw.get('is_async'):
        return p
    if p.wait() != 0:
        raise SystemExit(p.wait())


def ensure_vm(name):
    vm_cmd(name, 'run', name)
    wait_for_ssh(name)


def shutdown_vm(name):
    vm_cmd(name, 'shutdown', name)


class Rsync(object):

    excludes = frozenset({
        '*.pyc', '*.pyo', '*.swp', '*.swo', '*.pyj-cached', '*~', '.git'})

    def __init__(self, name):
        self.name = name

    def from_vm(self, from_, to, excludes=frozenset()):
        f = VM_SERVER + ':' + from_
        self(f, to, excludes)

    def to_vm(self, from_, to, excludes=frozenset()):
        t = VM_SERVER + ':' + to
        subprocess.check_call(
            ssh_to_vm(self.name) + [VM_SERVER, 'mkdir', '-p', to])
        self(from_, t, excludes)

    def __call__(self, from_, to, excludes=frozenset()):
        ssh = shlex.join(ssh_to_vm(self.name))
        if isinstance(excludes, type('')):
            excludes = excludes.split()
        excludes = frozenset(excludes) | self.excludes
        excludes = ['--exclude=' + x for x in excludes]
        cmd = [
            'rsync', '-a', '-zz', '-e', ssh, '--delete', '--delete-excluded'
        ] + excludes + [from_ + '/', to]
        # print(' '.join(cmd))
        print('Syncing', from_, flush=True)
        p = subprocess.Popen(cmd)
        if p.wait() != 0:
            q = shlex.join(cmd)
            raise SystemExit(
                f'The cmd {q} failed with error code: {p.returncode}')


def to_vm(rsync, sources_dir, pkg_dir, prefix='/', name='sw'):
    print('Mirroring data to the VM...', flush=True)
    prefix = prefix.rstrip('/') + '/'
    src_dir = os.path.dirname(base_dir())
    if os.path.exists(os.path.join(src_dir, 'setup.py')):
        excludes = get_rsync_conf()['to_vm_excludes']
        rsync.to_vm(src_dir, prefix + 'src', '/bypy/b ' + excludes)

    base = os.path.dirname(os.path.abspath(__file__))
    rsync.to_vm(os.path.dirname(base), prefix + 'bypy')
    rsync.to_vm(sources_dir, prefix + 'sources')
    rsync.to_vm(pkg_dir, prefix + name + '/pkg')
    if 'PENV' in os.environ:
        code_signing = os.path.expanduser(os.path.join(
            os.environ['PENV'], 'code-signing'))
        if os.path.exists(code_signing):
            rsync.to_vm(code_signing, '~/code-signing')


def from_vm(rsync, sources_dir, pkg_dir, output_dir, prefix='/', name='sw'):
    print('Mirroring data from VM...', flush=True)
    run_in_vm(rsync.name, 'rm', '-rf', '~/code-signing')
    prefix = prefix.rstrip('/') + '/'
    rsync.from_vm(prefix + name + '/dist', output_dir)
    rsync.from_vm(prefix + 'sources', sources_dir)
    rsync.from_vm(prefix + name + '/pkg', pkg_dir)


def run_main(name, *cmd):
    run_in_vm(name, *cmd)
