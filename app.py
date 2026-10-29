"""跑步数据注入器 —— 图形界面

单文件程序入口。tkinter 实现，逻辑全部在 core.py。
"""
import json
import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import core

APP_TITLE = 'MuMu 拟真跑步数据注入器'


def detect_mumu():
    """自动探测 MuMu 安装目录，不依赖任何写死的路径。

    顺序：注册表卸载项 → 常见安装位置 → PATH 上的 adb 反推 → 空。
    返回值是"含 nx_main 子目录"的那一层，找不到就返回空串。
    """
    def has_adb(d):
        return bool(d) and os.path.isfile(os.path.join(d, 'nx_main', 'adb.exe'))

    # 1) 注册表里 MuMu 的安装路径
    for hive in ('HKLM', 'HKCU'):
        for key in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                    r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
            try:
                res = core.quiet_run(
                    ['reg', 'query', f'{hive}\\{key}', '/s', '/f', 'MuMu'],
                    timeout=10)
                out = core.decode(res.stdout)
            except Exception:
                continue
            for line in out.splitlines():
                if 'InstallLocation' not in line and 'DisplayIcon' not in line:
                    continue
                _, _, val = line.partition('REG_SZ')
                val = val.strip().strip('"')
                if not val:
                    continue
                # DisplayIcon 指向 exe，取所在目录
                base = val if os.path.isdir(val) else os.path.dirname(val)
                for cand in (base, os.path.dirname(base)):
                    if has_adb(cand):
                        return cand

    # 2) 常见安装根目录下扫一遍
    roots = [os.environ.get(v) for v in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432')]
    roots += [os.path.join(os.environ.get('SystemDrive', 'C:') + os.sep, n)
              for n in ('Program Files', 'Program Files (x86)')]
    for root in filter(None, roots):
        for name in ('Netease', 'MuMu', 'MuMu Player 12', 'MuMuPlayer'):
            base = os.path.join(root, name)
            if has_adb(base):
                return base
            if os.path.isdir(base):
                for sub in os.listdir(base):
                    if has_adb(os.path.join(base, sub)):
                        return os.path.join(base, sub)

    # 3) PATH 上已有 adb 时，反推 nx_main 的父目录
    which = shutil.which('adb')
    if which:
        base = os.path.dirname(os.path.dirname(os.path.abspath(which)))
        if has_adb(base):
            return base

    return ''


class FlatButton(tk.Label):
    """tk.Button 的背景色在 Windows 上会被系统主题覆盖，用 Label 模拟扁平按钮。"""

    def __init__(self, parent, text, command, bg, hover):
        super().__init__(parent, text=text, bg=bg, fg='white',
                         font=('Microsoft YaHei UI', 10, 'bold'),
                         cursor='hand2', anchor='center',
                         relief='flat', borderwidth=0, highlightthickness=0)
        self._bg, self._hover, self._cmd = bg, hover, command
        self._enabled = True
        self.bind('<Button-1>', lambda e: self._emit())
        self.bind('<Enter>', lambda e: self._paint(self._hover) if self._enabled else None)
        self.bind('<Leave>', lambda e: self._paint(self._bg) if self._enabled else None)

    def _paint(self, color):
        self.config(bg=color)

    def _emit(self):
        if self._enabled and self._cmd:
            self._cmd()

    def set_enabled(self, on):
        self._enabled = on
        self.config(cursor='hand2' if on else 'arrow')
        self.config(bg=self._bg if on else '#3a3a42',
                    fg='white' if on else '#7a7a80')


def base_dir():
    """程序自身所在目录（.pyz 或源码同目录）。"""
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 资源读取
# 程序可能是三种形态，资源查找要都支持：
#   1) 源码目录：impl_kona.so 就在 app.py 旁边
#   2) .pyz     ：资源被打进 zip 归档，需从归档内读
#   3) EXE      ：解压后的 app 目录里有文件，等同源码目录
def in_archive():
    """当前是否运行在 zip 归档（.pyz）里。"""
    return bool(getattr(sys, 'frozen', False)) or os.path.isfile(
        os.path.abspath(__file__))


def archive_path():
    """返回 .pyz 的路径（若在归档内运行），否则空串。"""
    exe = getattr(sys, 'executable', '') or ''
    if exe.lower().endswith('.pyz'):
        return exe
    arg = sys.argv[0] if sys.argv else ''
    if arg.lower().endswith('.pyz'):
        return os.path.abspath(arg)
    return ''


def read_resource(name):
    """读资源，返回 (bytes 或 None, 来源描述)。

    顺序很重要：
      1) 若运行在 .pyz 归档里 —— 先查归档内部，保证用的是打包进去的那份，
         否则会被工作目录里同名的散件掩盖，出现"打包了却没用上"的假象
      2) 再查磁盘上的候选目录（源码/EXE 解压后的形态）
    """
    base = os.path.basename(name)

    # a) 归档内部（.pyz 形态优先，避免被同名散件盖住）
    zp = archive_path()
    if zp and os.path.isfile(zp):
        import zipfile
        try:
            with zipfile.ZipFile(zp) as z:
                names = z.namelist()
                for entry in (name, base):
                    if entry in names:
                        return z.read(entry), f'{zp}!/{entry}'
                for entry in names:
                    if os.path.basename(entry) == base:
                        return z.read(entry), f'{zp}!/{entry}'
        except Exception:
            pass

    # b) 磁盘上的若干候选目录
    tried = [
        base_dir(),
        os.path.dirname(base_dir()),
        os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else '',
        os.getcwd(),
    ]
    seen = set()
    for d in tried:
        if not d or d in seen:
            continue
        seen.add(d)
        for cand in (os.path.join(d, name), os.path.join(d, base),
                     os.path.join(d, 'probe', base),
                     os.path.join(os.path.dirname(d), name)):
            if os.path.isfile(cand):
                with open(cand, 'rb') as f:
                    return f.read(), cand

    return None, ''


def load_so():
    """读取 impl_kona.so；返回 (内容或 None, 来源描述)。"""
    data, src = read_resource('impl_kona.so')
    return data, src or '(未找到 impl_kona.so)'


def find_probe_apk():
    """定位探针 APK；若来自 zip 归档则先落盘再返回路径（adb install 需要真实文件）。"""
    data, src = read_resource('probe.apk')
    if not data:
        return ''
    # 来源是磁盘上的真实文件就直接用
    if '!/' not in src and os.path.isfile(src):
        return src
    # 来自归档：解压到临时文件
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix='probe_', suffix='.apk')
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    return tmp


def data_dir():
    """配置写到 exe/脚本同级目录；该处不可写时退回用户目录。"""
    here = base_dir()
    if os.access(here, os.W_OK):
        return here
    fallback = os.path.join(os.path.expanduser('~'), '.mumu-runner')
    os.makedirs(fallback, exist_ok=True)
    return fallback


class App:
    def __init__(self, root):
        self.root = root
        self.cfg_path = os.path.join(data_dir(), 'config.json')
        self.log_q = queue.Queue()
        self.worker = None
        self.session = None

        root.title(APP_TITLE)
        root.geometry('720x620')
        root.configure(bg='#1e1e22')
        root.resizable(False, False)

        self._build_ui()
        self._load_cfg()
        self.root.after(120, self._drain_log)

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        bg, fg = '#1e1e22', '#e8e8ea'
        box = '#2d2d34'

        def label(text, x, y, w=120, **kw):
            lab = tk.Label(self.root, text=text, bg=bg, fg='#d0d0d4',
                           anchor='w', font=('Microsoft YaHei UI', 9), **kw)
            lab.place(x=x, y=y, width=w, height=22)
            return lab

        # MuMu 路径
        label('MuMu 安装目录', 20, 22, 110)
        self.mumu_var = tk.StringVar()
        self.mumu_entry = tk.Entry(self.root, textvariable=self.mumu_var, bg=box, fg=fg,
                                   insertbackground=fg, relief='flat',
                                   font=('Consolas', 9))
        self.mumu_entry.place(x=135, y=19, width=420, height=26)
        self.mumu_var.trace_add('write', lambda *_: self._check_adb())

        tk.Button(self.root, text='浏览', command=self._browse, bg='#464650', fg='white',
                  relief='flat', font=('Microsoft YaHei UI', 9),
                  activebackground='#565660').place(x=565, y=18, width=60, height=27)

        self.adb_state = tk.Label(self.root, text='', bg=bg, fg='#888',
                                  anchor='w', font=('Microsoft YaHei UI', 8))
        self.adb_state.place(x=135, y=48, width=490, height=20)

        # 目标实例（多开时可选）
        label('目标实例', 20, 78, 110)
        self.target_var = tk.StringVar(value='全部运行中的实例')
        self.target_box = tk.OptionMenu(self.root, self.target_var, '全部运行中的实例')
        self.target_box.config(bg=box, fg=fg, relief='flat', highlightthickness=0,
                               activebackground='#3a3a44', activeforeground=fg,
                               font=('Microsoft YaHei UI', 9), anchor='w',
                               borderwidth=0)
        self.target_box['menu'].config(bg=box, fg=fg, font=('Microsoft YaHei UI', 9))
        self.target_box.place(x=135, y=75, width=230, height=26)

        self.btn_refresh = tk.Button(self.root, text='刷新', command=self._refresh_targets,
                                     bg='#464650', fg='white', relief='flat',
                                     font=('Microsoft YaHei UI', 9),
                                     activebackground='#565660')
        self.btn_refresh.place(x=372, y=74, width=56, height=28)

        self.target_hint = tk.Label(self.root, text='', bg=bg, fg='#7a7a80', anchor='w',
                                    font=('Microsoft YaHei UI', 8))
        self.target_hint.place(x=435, y=78, width=270, height=20)

        # 时长 / 步频
        label('时长（分钟）', 20, 112, 110)
        self.min_var = tk.IntVar(value=10)
        tk.Spinbox(self.root, from_=1, to=720, textvariable=self.min_var, bg=box, fg=fg,
                   relief='flat', font=('Consolas', 10), insertbackground=fg,
                   buttonbackground='#464650').place(x=135, y=109, width=95, height=26)

        label('步频（步/分）', 250, 112, 110)
        self.cad_var = tk.IntVar(value=174)
        tk.Spinbox(self.root, from_=60, to=260, textvariable=self.cad_var, bg=box, fg=fg,
                   relief='flat', font=('Consolas', 10), insertbackground=fg,
                   buttonbackground='#464650').place(x=365, y=109, width=95, height=26)

        hint = tk.Label(self.root, text='常用：慢跑 150-165，正常 170-180，快跑 185+',
                        bg=bg, fg='#7a7a80', anchor='w', font=('Microsoft YaHei UI', 8))
        hint.place(x=250, y=138, width=400, height=20)

        # 按钮
        self.btn_start = FlatButton(self.root, '▶  启 动', self.on_start,
                                    '#0d8f66', '#12a87a')
        self.btn_start.place(x=20, y=168, width=200, height=48)

        self.btn_stop = FlatButton(self.root, '■  停 止', self.on_stop,
                                   '#b34a2a', '#c85a36')
        self.btn_stop.place(x=235, y=168, width=200, height=48)

        self.btn_reset = FlatButton(self.root, '↺  重 置', self.on_reset,
                                    '#4a4a54', '#5a5a66')
        self.btn_reset.place(x=450, y=168, width=175, height=48)

        self.state = tk.Label(self.root, text='● 未运行', bg=bg, fg='#888',
                              anchor='w', font=('Microsoft YaHei UI', 11, 'bold'))
        self.state.place(x=20, y=228, width=400, height=24)

        label('日志', 20, 262, 60)

        self.log = tk.Text(self.root, bg='#141418', fg='#7ee787', relief='flat',
                           font=('Consolas', 9), wrap='word', state='disabled',
                           insertbackground=fg)
        self.log.place(x=20, y=284, width=665, height=273)
        sb = tk.Scrollbar(self.root, command=self.log.yview, bg=box)
        sb.place(x=685, y=284, width=14, height=273)
        self.log.config(yscrollcommand=sb.set)

    # ------------------------------------------------------------ 配置
    def _load_cfg(self):
        # 没有配置时自动探测 MuMu 位置，避免任何写死的路径
        cfg = {'mumu': '', 'minutes': 10, 'cadence': 174}
        if os.path.isfile(self.cfg_path):
            try:
                cfg.update(json.load(open(self.cfg_path, encoding='utf-8')))
            except Exception:
                pass
        mumu = cfg.get('mumu') or ''
        if not os.path.isfile(os.path.join(mumu, 'nx_main', 'adb.exe')):
            mumu = detect_mumu() or mumu
        self.mumu_var.set(mumu)
        self.min_var.set(int(cfg.get('minutes', 10)))
        self.cad_var.set(int(cfg.get('cadence', 174)))
        self._check_adb()
        if mumu:
            self.put(f'就绪。MuMu 目录: {mumu}')
            # 启动时就扫描一次实例，避免用户以为只有一个
            self.root.after(200, self._refresh_targets)
        else:
            self.put('未能自动找到 MuMu，请点「浏览」指定安装目录')

    def _save_cfg(self):
        try:
            json.dump({'mumu': self.mumu_var.get(),
                       'minutes': self.min_var.get(),
                       'cadence': self.cad_var.get()},
                      open(self.cfg_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        except Exception as e:
            self.put(f'配置保存失败: {e}')

    def _adb_path(self):
        return os.path.join(self.mumu_var.get(), 'nx_main', 'adb.exe')

    def _check_adb(self):
        p = self._adb_path()
        if os.path.isfile(p):
            self.adb_state.config(text='✓ 已找到 adb', fg='#3fb950')
            return p
        self.adb_state.config(text=f'✗ 未找到 {p}', fg='#f85149')
        return None

    def _browse(self):
        start = self.mumu_var.get()
        if not os.path.isdir(start):
            # 从桌面/用户目录起步，而不是写死盘符
            start = os.path.expanduser('~')
        d = filedialog.askdirectory(title='选择 MuMu 安装目录（含 nx_main）',
                                    initialdir=start)
        if d:
            self.mumu_var.set(os.path.normpath(d))
            self.put(f'MuMu 目录: {d}')
            self._refresh_targets()

    # ------------------------------------------------------------ 实例
    ALL_TARGET = '全部运行中的实例'

    def _refresh_targets(self):
        """扫描本机 MuMu 实例，填充下拉框。"""
        adb = self._check_adb()
        menu = self.target_box['menu']
        menu.delete(0, 'end')
        self._instances = []
        if not adb:
            self.target_hint.config(text='未找到 adb', fg='#f85149')
            return
        try:
            items = core.list_instances(adb)
        except Exception as e:
            self.target_hint.config(text=f'枚举失败: {e}', fg='#f85149')
            return
        self._instances = items
        running = [x for x in items if x['started']]
        menu.add_command(label=self.ALL_TARGET,
                         command=lambda: self.target_var.set(self.ALL_TARGET))
        for x in items:
            label = f"实例 {x['index']}  ({x['device']})" + ('' if x['started'] else '  [未启动]')
            menu.add_command(label=label, command=lambda v=label: self.target_var.set(v))
        # 选择失效时回到「全部」
        if self.target_var.get() not in [self.ALL_TARGET] + [
                f"实例 {x['index']}  ({x['device']})" + ('' if x['started'] else '  [未启动]')
                for x in items]:
            self.target_var.set(self.ALL_TARGET)
        self.target_hint.config(
            text=f'{len(running)} 个运行中 / 共 {len(items)} 个',
            fg='#3fb950' if running else '#f85149')

    def _selected_targets(self):
        """按当前下拉选择返回要操作的实例列表。"""
        items = getattr(self, '_instances', [])
        running = [x for x in items if x['started']]
        sel = self.target_var.get()
        if sel == self.ALL_TARGET:
            return running
        for x in items:
            label = (f"实例 {x['index']}  ({x['device']})"
                     + ('' if x['started'] else '  [未启动]'))
            if label == sel:
                return [x]
        return running

    # ------------------------------------------------------------ 日志
    def put(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log.config(state='normal')
                self.log.insert('end', f'[{__import__("time").strftime("%H:%M:%S")}] {msg}\n')
                self.log.see('end')
                self.log.config(state='disabled')
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _set_running(self, running):
        self.state.config(text='● 注入运行中' if running else '● 未运行',
                          fg='#3fb950' if running else '#888')
        self.btn_start.set_enabled(not running)

    def _busy(self, on):
        for b in (self.btn_start, self.btn_stop, self.btn_reset):
            b.set_enabled(not on)

    # ------------------------------------------------------------ 动作
    def _run_async(self, fn):
        if self.worker and self.worker.is_alive():
            self.put('上一个操作还在进行中')
            return

        def wrapper():
            self._busy(True)
            try:
                fn()
            except Exception as e:
                self.put(f'✗ {e}')
                self._set_running(False)
            finally:
                self._busy(False)

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def on_start(self):
        adb = self._check_adb()
        if not adb:
            messagebox.showerror('路径错误', 'MuMu 目录无效，请确认包含 nx_main\\adb.exe')
            return
        self._save_cfg()
        minutes = max(1, int(self.min_var.get()))
        cadence = max(60, int(self.cad_var.get()))
        targets = self._selected_targets()
        if not targets:
            messagebox.showwarning('没有可用实例', '未发现运行中的模拟器，请先启动 MuMu。')
            return
        names = '、'.join(f"实例{x['index']}" for x in targets)
        self.put(f'启动：{minutes} 分钟，{cadence} 步/分 → {names}')

        def job():
            so_bytes, so_src = load_so()
            if so_bytes is None:
                raise RuntimeError(
                    '缺少 HAL 原文件 impl_kona.so。\n'
                    '请把它放在程序同目录，或重新用 build.py 打包进程序。')
            self.put(f'HAL 原文件: {so_src}')

            fleet = core.Fleet(adb, self.put)
            self.fleet = fleet

            # 1) 逐实例部署（并行会争抢 adb server，串行更稳）
            self.put(f'部署 HAL 补丁到 {len(targets)} 个实例…')
            res = fleet.deploy_all(so_bytes, items=targets, parallel=False)
            ok = [d for d, (s, _) in res.items() if s]
            if not ok:
                raise RuntimeError('所有实例部署失败')

            # 2) 定位环形缓冲（顺带把探针装到没有的实例上）
            self.put('定位环形缓冲…')
            apk = find_probe_apk()
            r2 = fleet.locate_rings(items=[x for x in targets if x['device'] in ok],
                                    apk_path=apk)
            good = [x for x in targets if x['device'] in ok and r2.get(x['device'], (False,))[0]]

            # 3) 各自校准帧率（不同实例性能可能不同）
            self.put('校准帧率…')
            c3 = fleet.calibrate_all(items=good)
            fps_list, sleep_map = [], {}
            for x in good:
                s_ok, val = c3.get(x['device'], (False, None))
                if s_ok and val:
                    sleep_map[x['device']] = val[0]
                    fps_list.append(val[1])
            if not sleep_map:
                raise RuntimeError('校准失败')
            fps = sum(fps_list) / len(fps_list)

            # 4) 生成同一份波形，推给所有实例
            ring, text = core.generate(fps, minutes * 60, cadence)
            frames = len(ring) // 36
            self.put(f'{frames} 帧 @ 平均 {fps:.1f}Hz，开始注入…')
            s4 = fleet.start_all(ring, text, frames, sleep_map, loop=True, items=good)
            started = [x for x in good if s4.get(x['device'], (False,))[0]]

            if started:
                names = '、'.join(f"实例{x['index']}" for x in started)
                self.put(f'✓ 注入运行中：{len(started)} 个实例（{names}），'
                         f'{frames} 帧 @ {fps:.1f}Hz，循环')
                self.root.after(0, lambda: self._set_running(True))
            else:
                raise RuntimeError('注入启动失败')

        self._run_async(job)

    def on_stop(self):
        adb = self._check_adb()
        if not adb:
            return
        targets = self._selected_targets()
        self.put(f'停止注入（{len(targets)} 个实例）')

        def job():
            core.Fleet(adb, self.put).stop_all(items=targets, parallel=False)
            self.root.after(0, lambda: self._set_running(False))

        self._run_async(job)

    def on_reset(self):
        adb = self._check_adb()
        if not adb:
            return
        targets = self._selected_targets()
        if not messagebox.askyesno(
                '重置',
                f'这会停止注入并还原官方 HAL。\n'
                f'影响 {len(targets)} 个实例：'
                f'{"、".join("实例" + x["index"] for x in targets)}\n\n确定要继续吗？'):
            return
        self.put('重置：停止注入并还原官方 HAL')

        def job():
            core.Fleet(adb, self.put).restore_all(items=targets, parallel=False)
            self.put('✓ 已还原')
            self.root.after(0, lambda: self._set_running(False))

        self._run_async(job)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
