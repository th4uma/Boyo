"""Repair a moved Windows environment offline, retaining installed packages."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def ensure(local):
    local = Path(local).resolve()
    base = local / 'python'
    environment = local / 'environment'
    if Path(sys.base_prefix).resolve() != base:
        raise RuntimeError('请使用 local/python 中随工具携带的 Python 执行修复')
    if not (environment / 'Lib/site-packages/pip').is_dir():
        raise RuntimeError('缺少已安装依赖，请先运行安装环境.ps1')
    stamp = environment / '.boyo-location'
    previous_stamp = environment / '.liuji-location'  # Upgrade an existing environment without reinstalling it.
    if stamp.is_file() and stamp.read_text(encoding='utf-8').strip() == str(local):
        previous_stamp.unlink(missing_ok=True)
        return False
    if previous_stamp.is_file() and previous_stamp.read_text(encoding='utf-8').strip() == str(local):
        previous_stamp.replace(stamp)
        return False
    import venv
    venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
    python = environment / 'Scripts/python.exe'
    probe = subprocess.run([str(python), '-c', 'import json,sys;print(json.dumps([sys.prefix,sys.base_prefix]))'],
                           check=True, capture_output=True, text=True)
    prefix, base_prefix = json.loads(probe.stdout)
    if Path(prefix).resolve() != environment or Path(base_prefix).resolve() != base:
        raise RuntimeError('环境路径修复验证失败')
    # Windows console launchers embed interpreter paths; regenerate without pip/network.
    script = r'''
import importlib.metadata as metadata
import sys
from pathlib import Path
from pip._vendor.distlib.scripts import ScriptMaker
maker = ScriptMaker(None, str(Path(sys.prefix)/'Scripts'))
maker.executable = sys.executable
maker.clobber = True
maker.variants = {''}
for distribution in metadata.distributions():
    for point in distribution.entry_points:
        if point.group in ('console_scripts', 'gui_scripts'):
            maker.make(point.name+' = '+point.value, {'gui':point.group=='gui_scripts'})
for name in ('pip3', 'pip'+str(sys.version_info.major)+'.'+str(sys.version_info.minor)):
    maker.make(name+' = pip._internal.cli.main:main')
'''
    subprocess.run([str(python), '-c', script], check=True)
    stamp.write_text(str(local)+'\n', encoding='utf-8')
    previous_stamp.unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent
    local = repo.parent / 'local'
    os.environ['BOYO_LOCAL'] = str(local)
    for key, value in {'TEMP':local/'tmp', 'TMP':local/'tmp',
                       'PYTHONPYCACHEPREFIX':local/'cache/pycache'}.items():
        Path(value).mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(value)
    sys.pycache_prefix = str(local/'cache/pycache')
    ensure(local)
    sys.path.insert(0, str(repo/'src'))
    from boyo.config import Runtime
    from boyo.portability import repair_workspace
    runtime = Runtime(local)
    runtime.prepare()
    if args.workspace:
        repair_workspace(runtime, args.workspace)
    else:
        runtime.settings()  # Relocation recognition runs once, using bounded photo probes.


if __name__ == '__main__':
    main()
