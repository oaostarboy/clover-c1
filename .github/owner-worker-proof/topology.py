"""Exact native venv-launcher/base-worker topology used by the proven PS probe.

Task Scheduler does not inherit the driver's environment. The worker may name
its base image in argv[0], so environment/argv-prefix filtering is not ownership.
An exact installed launcher image, direct parent link, exact measured base image
and the caller's separately checked state PID/birth/listener are all required.
"""
from pathlib import PureWindowsPath


def image(path):
    p = PureWindowsPath(path)
    assert p.is_absolute(), 'absolute native image required'
    return str(p).casefold()


def pair(processes, installed_python, measured_base):
    launchers = {image(installed_python), image(str(PureWindowsPath(installed_python).with_name('pythonw.exe')))}
    bases = {image(measured_base), image(str(PureWindowsPath(measured_base).with_name('pythonw.exe')))}
    roots = [row for row in processes if image(row['exe']) in launchers]
    assert len(roots) == 1, dict(reason='one exact installed launcher required', roots=roots)
    root = roots[0]
    children = [row for row in processes if row['ppid'] == root['pid']]
    if children:
        assert len(children) == 1, dict(reason='one direct interpreter worker required', children=children)
        worker = children[0]
        assert image(worker['exe']) in bases, dict(reason='worker image differs from measured base', worker=worker, bases=sorted(bases))
        return [root, worker]
    # A venv implementation without a launcher child can own its listener
    # directly. State PID/birth/listener equality is still mandatory upstream.
    return [root]
