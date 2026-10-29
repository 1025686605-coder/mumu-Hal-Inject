"""把应用打包成单个 EXE。

做法：
  1. 精简收集 Python 运行时（解释器 + 必需标准库 + tkinter + pyelftools）
  2. 与应用脚本、HAL 原文件、探针 APK 一起压成 payload.zip
  3. 用系统自带 csc.exe 把 launcher.cs 编译成 exe，zip 作为资源嵌入

产物：dist/MuMu跑步注入器.exe（约 15-20 MB），目标机器无需安装 Python。

用法: python build.py
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipapp
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, '.build')
DIST = os.path.join(HERE, 'dist')


def find_csc():
    """自动定位 .NET Framework 的 C# 编译器，不写死路径。"""
    # 1) 优先问 .NET 自己的安装目录
    windir = os.environ.get('WINDIR') or os.environ.get('SystemRoot')
    if not windir:
        # 没有环境变量时从系统盘推断，仍不写死具体盘符
        drive = os.environ.get('SystemDrive') or os.path.splitdrive(sys.executable)[0]
        windir = os.path.join(drive + os.sep, 'Windows') if drive else ''
    mscorcfg = os.path.join(windir, 'Microsoft.NET', 'Framework64') if windir else ''
    if mscorcfg and os.path.isdir(mscorcfg):
        # 版本号取最大的那个（v4.0.30319 通常就是最新）
        vers = sorted((d for d in os.listdir(mscorcfg) if d.lower().startswith('v')),
                      reverse=True)
        for v in vers:
            p = os.path.join(mscorcfg, v, 'csc.exe')
            if os.path.isfile(p):
                return p
    # 2) 32 位回退
    if windir:
        for base in ('Framework', 'Framework64'):
            p = os.path.join(windir, 'Microsoft.NET', base, 'v4.0.30319', 'csc.exe')
            if os.path.isfile(p):
                return p
    # 3) PATH 上找
    which = shutil.which('csc') or shutil.which('csc.exe')
    if which:
        return which
    return ''

# 必须随包携带的标准库（tkinter/ctypes 为 GUI 与 pyelftools 依赖）
STDLIB = [
    'tkinter', 'json', 'queue', 'threading', 'subprocess', 'tempfile', 'struct',
    'math', 're', 'os', 'sys', 'io', 'time', 'encodings', 'collections',
    'functools', 'itertools', 'ctypes', 'email', 'http', 'urllib', 'concurrent',
    'logging', 'warnings', 'contextlib', 'weakref', 'types', 'copy',
    'csv', 'hashlib', 'base64', 'datetime', 'pickle', 'traceback', 'linecache',
]
# DLLs 目录下必须的二进制扩展
DLLS = ['_ctypes.pyd', 'unicodedata.pyd', '_tkinter.pyd', 'tcl86t.dll', 'tk86t.dll',
        '_socket.pyd', 'select.pyd', 'libffi-8.dll', 'zlib1.dll', '_ssl.pyd',
        'libcrypto-3-x64.dll', 'libssl-3-x64.dll']

APP_FILES = ['app.py', 'core.py', 'impl_kona.so']


def find_python():
    return os.path.dirname(sys.executable)


def copy_runtime(pydir, dest):
    """把精简运行时复制到 dest/runtime。"""
    rt = os.path.join(dest, 'runtime')
    os.makedirs(rt, exist_ok=True)

    for f in ('python.exe', 'pythonw.exe', 'python314.dll', 'python3.dll'):
        src = os.path.join(pydir, f)
        if os.path.exists(src):
            shutil.copy2(src, rt)
        elif f.startswith('python'):
            raise SystemExit(f'缺少关键文件 {f}')

    # 全部二进制扩展（.pyd / .dll）放运行时根目录。
    # 逐项列举容易漏掉间接依赖（如 pyelftools 需要的 _overlapped），整目录复制更稳。
    dll_src = os.path.join(pydir, 'DLLs')
    if os.path.isdir(dll_src):
        for f in os.listdir(dll_src):
            p = os.path.join(dll_src, f)
            if os.path.isfile(p) and f.lower().endswith(('.pyd', '.dll')):
                shutil.copy2(p, rt)

    # 根目录下的运行库 dll（vcruntime 等）
    for f in os.listdir(pydir):
        p = os.path.join(pydir, f)
        if os.path.isfile(p) and f.lower().endswith('.dll'):
            shutil.copy2(p, rt)

    # tkinter 需要 tcl/tk 数据目录
    for d in ('tcl', 'tk'):
        src = os.path.join(pydir, 'tcl')
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(rt, 'tcl'), dirs_exist_ok=True)
            break

    # 标准库：复制整个 Lib（排除测试与缓存），比逐项挑选可靠得多
    libdst = os.path.join(rt, 'Lib')
    shutil.copytree(os.path.join(pydir, 'Lib'), libdst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        '__pycache__', 'test', 'tests', 'idlelib', 'lib2to3',
                        'distutils', 'site-packages', 'ensurepip', 'venv',
                        '*.pyc', '*.pyo'))
    # site-packages 里的 pyelftools
    for sp in ('Lib/site-packages', 'site-packages'):
        src = os.path.join(pydir, sp, 'elftools')
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(libdst, 'elftools'), dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
            break
    else:
        raise SystemExit('未找到 elftools，请先 pip install pyelftools')

    # 关键：把 Lib 与 app 目录显式加入模块搜索路径。
    # 出了标准安装布局，Python 找不到 Lib；且 ._pth 一旦存在会忽略 PYTHONPATH，
    # 所以 app 目录也必须写进来，否则 import core 会失败。
    ver = 'python%d%d' % sys.version_info[:2]
    with open(os.path.join(rt, f'{ver}._pth'), 'w', encoding='ascii') as f:
        f.write(f'{ver}.zip\n.\nLib\nLib\\site-packages\n..\\app\nimport site\n')


def copy_app(dest):
    app = os.path.join(dest, 'app')
    os.makedirs(app, exist_ok=True)
    for f in APP_FILES:
        src = os.path.join(HERE, f)
        if not os.path.exists(src):
            raise SystemExit(f'缺少 {f}')
        shutil.copy2(src, app)
    # probe.apk 可选（自动安装用）
    probe = os.path.join(HERE, 'probe', 'probe.apk')
    if os.path.exists(probe):
        shutil.copy2(probe, app)


def make_zip(work, out):
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, _, files in os.walk(work):
            for f in files:
                p = os.path.join(base, f)
                z.write(p, os.path.relpath(p, work).replace('\\', '/'))


def build_exe(zip_path, out_exe):
    """编译启动器，然后把 payload.zip 追加到 exe 末尾。

    不用 .NET 资源嵌入：14MB 的资源会让 winexe 在 Main 之前加载失败。
    overlay 结构：[exe][zip][8B 长度][8B 魔数]。
    """
    csc = find_csc()
    if not csc:
        raise SystemExit('未找到 csc.exe（需要 .NET Framework 4.x）。'
                         '可只构建 .pyz：python build.py --pyz-only')
    print(f'  csc = {csc}')
    cmd = [csc, '/nologo', '/target:winexe', '/optimize+',
           '/r:System.Windows.Forms.dll',
           f'/out:{out_exe}', os.path.join(HERE, 'launcher.cs')]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit('csc 编译失败:\n' + (r.stdout + r.stderr))
    if r.stdout.strip():
        print(r.stdout.strip())

    # 追加 payload
    with open(zip_path, 'rb') as f:
        data = f.read()
    with open(out_exe, 'ab') as f:
        f.write(data)
        f.write(struct.pack('<q', len(data)))
        f.write(struct.pack('<q', 0x4D554D555A49505A))   # "MUMUZIP"


def build_pyz(out_pyz):
    """单文件 .pyz（标准库 zipapp），目标机器需有 Python。"""
    stage = os.path.join(BUILD, 'pyz')
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    for f in ('app.py', 'core.py'):
        shutil.copy2(os.path.join(HERE, f), stage)
    zipapp.create_archive(stage, out_pyz, main='app:main')
    return out_pyz


def main():
    pydir = find_python()
    print(f'Python: {pydir}')

    # --pyz-only：只产出 26KB 的 .pyz，不构建 EXE（无需 .NET、速度快）
    pyz_only = '--pyz-only' in sys.argv

    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD, exist_ok=True)
    os.makedirs(DIST, exist_ok=True)

    pyz = os.path.join(DIST, 'mumu_runner.pyz')
    build_pyz(pyz)
    print(f'pyz = {pyz}  ({os.path.getsize(pyz)//1024} KB)')
    if pyz_only:
        return

    stage = os.path.join(BUILD, 'stage')
    os.makedirs(stage)

    print('收集运行时…')
    copy_runtime(pydir, stage)
    print('收集应用…')
    copy_app(stage)

    # 去掉 pycache
    for base, dirs, _ in os.walk(stage):
        for d in list(dirs):
            if d == '__pycache__':
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
                dirs.remove(d)

    zip_path = os.path.join(BUILD, 'payload.zip')
    print('打包 payload…')
    make_zip(stage, zip_path)
    mb = os.path.getsize(zip_path) / 1048576
    print(f'  payload.zip = {mb:.1f} MB')

    out_exe = os.path.join(DIST, 'MuMu跑步注入器.exe')
    print('编译 EXE…')
    build_exe(zip_path, out_exe)

    size = os.path.getsize(out_exe) / 1048576
    print(f'\nOK {out_exe}  ({size:.1f} MB)')


if __name__ == '__main__':
    main()
