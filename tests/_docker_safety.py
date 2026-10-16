"""Test-only container capability. Not a marker or environment guard bypass.

Creation is admitted only while the Docker fixture holds an exact argv grant.
The fixture pins the image, adds an unguessable ownership label, and restricts
mounts/namespaces. Exec/restart use inspected immutable IDs, not mutable names.
The ordinary host guard still handles all other commands and subprocess APIs.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
import shutil
import shlex


class DockerTestCapability:
    def __init__(self, run, image: str, private: Path):
        self.run = run
        self.docker = shutil.which('docker')
        self.script = shutil.which('script')
        if not self.docker:
            raise ValueError('Docker unavailable')
        self.private = private.resolve(strict=True)
        self.label = 'clover.test-owner=' + secrets.token_hex(24)
        self.grants = set()
        self.owned = {}  # names -> inspected immutable IDs
        self.volumes = set()
        self.created = set()
        info = self._inspect('image', image)
        self.image = info['Id']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', self.image):
            raise ValueError('unidentified Docker image')
        self.image_name = image

    def _inspect(self, kind, target):
        result = self.run([self.docker, kind, 'inspect', '--', target],
                          capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError('Docker inspection failed')
        data = json.loads(result.stdout)
        if not isinstance(data, list) or len(data) != 1:
            raise ValueError('ambiguous Docker target')
        return data[0]

    def _mount(self, value):
        fields = value.split(':')
        if len(fields) != 2 or fields[1] not in {'/opt/data', '/clean'}:
            raise ValueError('Docker mount refused')
        source = fields[0]
        if source.startswith('/'):
            path = Path(source)
            if (path.resolve(strict=True) != path or not path.is_dir()
                    or not path.is_relative_to(self.private) or path == self.private):
                raise ValueError('Docker bind is not private test data')
        elif source not in self.volumes:
            raise ValueError('Docker named volume is not test-owned')
        return value

    def prepare(self, cmd, args, options):
        if (type(cmd) is list and len(cmd) == 4 and self.script
                and cmd[0] in {'script', self.script} and cmd[1] == '-qc'
                and cmd[3] == '/dev/null'
                and self._plain([self.docker, 'run'], args, options)):
            inner = shlex.split(cmd[2])
            if len(inner) < 3 or inner[:2] != ['docker', 'run']:
                raise ValueError('PTY wrapper must run one Docker creation')
            prepared = self._create(inner)
            if '-d' in prepared:
                raise ValueError('PTY wrapper must not detach a container')
            # Serialize EVERY token, including the in-container shell script.
            # The host shell executes just this pinned, confined Docker command.
            return [self.script, '-qc', shlex.join(prepared), '/dev/null']
        if not self._plain(cmd, args, options):
            if self.involves_docker(cmd):
                raise ValueError('Docker subprocess override refused')
            return None
        verb = cmd[1] if len(cmd) > 1 else ''
        if verb == 'rm' or (verb in {'container', 'volume'} and cmd[2:3] == ['rm']):
            return self._removal(cmd)
        if verb == 'volume' and len(cmd) == 4 and cmd[2] == 'create':
            if not re.fullmatch(r'clover-test-volume-[0-9a-f]{32}', cmd[3]):
                raise ValueError('Docker volume name refused')
            return [self.docker, 'volume', 'create', '--label', self.label, cmd[3]]
        if verb == 'run':
            return self._create(cmd)
        if verb not in {'exec', 'restart'}:
            if not self.readonly(cmd):
                raise ValueError('ungranted Docker operation refused')
            return None
        index = 2
        if verb == 'exec':
            while index < len(cmd) and cmd[index].startswith('-'):
                if cmd[index] not in {'-u', '--user', '-e', '--env', '-w', '--workdir'} or index + 1 >= len(cmd):
                    raise ValueError('Docker exec option refused')
                index += 2
        if index >= len(cmd) or cmd[index] not in self.owned:
            raise ValueError('Docker target is not owned by this test')
        target = self.owned[cmd[index]]
        info = self._inspect('container', target)
        self._confined(info)
        if info['Id'] != target:
            raise ValueError('Docker identity changed')
        result = list(cmd)
        result[0], result[index] = self.docker, target
        if verb == 'restart' and index != len(result) - 1:
            raise ValueError('Docker restart target list refused')
        return result

    def _removal(self, cmd):
        volume = cmd[1:3] == ['volume', 'rm']
        index = 3 if cmd[1] in {'container', 'volume'} else 2
        flags = []
        while index < len(cmd) and cmd[index] in {'-f', '--force'}:
            flags.append(cmd[index])
            index += 1
        if index != len(cmd) - 1:
            raise ValueError('Docker removal target/options refused')
        target = cmd[index]
        if volume:
            if target not in self.volumes:
                raise ValueError('Docker removal volume is not owned')
            info = self._inspect('volume', target)
            if (info.get('Name') != target or info.get('Driver') != 'local' or info.get('Options')
                    or info.get('Labels', {}).get('clover.test-owner') != self.label.split('=', 1)[1]):
                raise ValueError('Docker removal volume ownership changed')
            return [self.docker, 'volume', 'rm', *flags, target]
        if target not in self.owned:
            raise ValueError('Docker removal container is not owned')
        # IDs cannot be recycled. Never resolve a mutable name during removal.
        return [self.docker, 'rm', *flags, self.owned[target]]

    def involves_docker(self, cmd):
        if isinstance(cmd, (list, tuple)):
            return any(re.search(r'(^|[\s/])docker(?:\s|$)', str(part)) for part in cmd)
        return bool(re.search(r'(^|[\s/])docker(?:\s|$)', str(cmd)))

    def readonly(self, cmd, args=(), options=None):
        return (self._plain(cmd, args, options or {})
                and (cmd[1] in {'info', 'inspect', 'logs', 'ps', 'version'}
                     or cmd[1:3] in [['container', 'inspect'], ['volume', 'inspect'], ['image', 'inspect']]))

    def _plain(self, cmd, args, options):
        return (type(cmd) is list and len(cmd) >= 2 and all(type(x) is str for x in cmd)
                and cmd[0] in {'docker', self.docker} and not args
                and not (options.keys() - {'capture_output', 'text', 'timeout', 'check',
                    'stdout', 'stderr', 'stdin', 'encoding', 'errors', 'shell', 'close_fds'})
                and options.get('shell', False) is False
                and options.get('close_fds', True) is True
                and options.get('stdin') is None)

    def _create(self, cmd):
        result = [self.docker, 'run', '--label', self.label]
        index = 2
        name = None
        mounts = []
        while index < len(cmd) and cmd[index].startswith('-'):
            flag = cmd[index]
            if flag in {'--rm', '-d', '--init', '-t'}:
                result.append(flag)
                index += 1
                continue
            if flag not in {'--name', '-e', '--entrypoint', '-v', '-u', '--user', '--network'} or index + 1 >= len(cmd):
                raise ValueError('Docker creation option refused')
            value = cmd[index + 1]
            if flag == '--name':
                if name is not None or not re.fullmatch(r'clover-(?:test|restart)-[A-Za-z0-9_.-]+', value):
                    raise ValueError('Docker name refused')
                name = value
                # Existing foreign names must never be adopted or replaced.
                if name in self.owned:
                    raise ValueError('Docker name already owned')
            if flag == '-v':
                mounts.append(self._mount(value))
            if flag == '--network' and value != 'none':
                raise ValueError('Docker network override refused')
            result.extend([flag, value])
            index += 2
        if index >= len(cmd) or cmd[index] not in {self.image_name, self.image}:
            raise ValueError('Docker image is not the pinned test image')
        if '-d' in result and name is None:
            raise ValueError('detached Docker creation requires an owned name')
        if '-d' not in result and '--rm' not in result:
            raise ValueError('foreground Docker creation requires automatic cleanup')
        result.extend([self.image, *cmd[index + 1:]])
        return result

    def _confined(self, info):
        host, config = info['HostConfig'], info['Config']
        if (config.get('Labels', {}).get('clover.test-owner') != self.label.split('=', 1)[1]
                or info.get('Image') != self.image
                or host.get('Privileged') or host.get('CapAdd') or host.get('Devices')
                or host.get('VolumesFrom') or host.get('SecurityOpt')
                or host.get('NetworkMode') == 'host' or host.get('PidMode')
                or host.get('IpcMode') == 'host' or host.get('UsernsMode') == 'host'
                or host.get('CgroupnsMode') == 'host'
                or host.get('Runtime') not in {None, '', 'runc'}):
            raise ValueError('Docker isolation proof refused')
        for mount in info.get('Mounts', []):
            if mount.get('Type') == 'bind':
                self._mount(mount['Source'] + ':' + mount['Destination'])
            elif mount.get('Type') != 'volume' or mount.get('Destination') != '/opt/data':
                raise ValueError('Docker mount inspection refused')

    def admit(self, name, cmd, args, options):
        pty = (type(cmd) is list and len(cmd) == 4 and getattr(self, 'script', None)
               and cmd[:2] == [self.script, '-qc'] and cmd[3] == '/dev/null'
               and self._plain([self.docker, 'run'], args, options))
        return (name in {'run', 'Popen'} and (self._plain(cmd, args, options) or pty)
                and tuple(cmd) in self.grants)

    def execute(self, guarded_run, cmd, *args, **options):
        prepared = self.prepare(cmd, args, options)
        if prepared is None:
            return guarded_run(cmd, *args, **options)
        grant = tuple(prepared)
        self.grants.add(grant)
        try:
            result = guarded_run(prepared, *args, **options)
            if prepared[1:3] == ['volume', 'create'] and result.returncode == 0:
                info = self._inspect('volume', prepared[-1])
                if info.get('Labels', {}).get('clover.test-owner') != self.label.split('=', 1)[1]:
                    raise ValueError('Docker volume is not owned')
                if info.get('Driver') != 'local' or info.get('Options'):
                    raise ValueError('Docker volume driver/options refused')
                self.volumes.add(prepared[-1])
            if prepared[1] == 'run' and result.returncode == 0 and '-d' in prepared:
                # Inspect by full ID returned by Docker, not a caller-provided name.
                target = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
                target = (target or '').strip()
                if not re.fullmatch(r'[0-9a-f]{64}', target):
                    raise ValueError('Docker creation did not return an immutable ID')
                self.created.add(target)
                info = self._inspect('container', target)
                self._confined(info)
                if info['Id'] != target:
                    raise ValueError('Docker creation identity mismatch')
                self.owned[target] = target
                self.owned[info['Name'].lstrip('/')] = target
            return result
        finally:
            self.grants.discard(grant)

    def cleanup(self):
        """Reap even setup failures, but never adopt an unowned target."""
        for target in self.created:
            result = self.run([self.docker, 'container', 'inspect', '--', target],
                              capture_output=True, text=True, timeout=10)
            if result.returncode:
                continue  # the ordinary fixture already removed it
            info = json.loads(result.stdout)[0]
            self._confined(info)
            if info['Id'] != target:
                raise ValueError('Docker cleanup identity changed')
            # A transient first inspection failure may precede registration.
            # Recovered exact-ID + ownership validation permits that ID only.
            self.owned[target] = target
            self.execute(self.run, [self.docker, 'rm', '-f', target], check=True,
                         capture_output=True, timeout=30)
        for volume in self.volumes:
            result = self.run([self.docker, 'volume', 'inspect', '--', volume],
                              capture_output=True, text=True, timeout=10)
            if result.returncode:
                continue
            info = json.loads(result.stdout)[0]
            if info.get('Labels', {}).get('clover.test-owner') != self.label.split('=', 1)[1]:
                raise ValueError('Docker cleanup volume ownership changed')
            self.execute(self.run, [self.docker, 'volume', 'rm', volume], check=True,
                         capture_output=True, timeout=30)
