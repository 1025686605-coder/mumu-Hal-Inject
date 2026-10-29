"""跑步数据注入器 —— 核心逻辑（HAL 补丁 / 环形缓冲定位 / 波形 / 注入）

设计要点见 README。这里把所有能力收进一个模块，GUI 与命令行共用。
"""
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------- 静默执行
# 程序以 pythonw 启动（无控制台）。此时每次启动子进程（adb/reg）Windows 都会
# 新建一个控制台窗口并闪一下，多开时尤其明显。统一加 CREATE_NO_WINDOW 消除。
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008


def quiet_run(cmd, timeout=30, cwd=None, shell=False):
    """执行外部命令但完全不弹窗口，返回 CompletedProcess。

    三处组合才能压住所有情况：
      1) DETACHED_PROCESS —— 子进程不继承/不新建控制台（adb 会自己建，必须用它）
      2) CREATE_NO_WINDOW —— 兜底，防止个别程序仍分配控制台
      3) STARTUPINFO + SW_HIDE —— 子进程自己画的窗口也隐藏
    只加 CREATE_NO_WINDOW 时 adb 仍会闪一个窗口。
    """
    kwargs = {
        'capture_output': True,
        'timeout': timeout,
        'cwd': cwd,
        'shell': shell,
    }
    if os.name == 'nt':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0          # SW_HIDE
        kwargs['startupinfo'] = si
        kwargs['creationflags'] = _CREATE_NO_WINDOW | _DETACHED_PROCESS
        kwargs['stdin'] = subprocess.DEVNULL
    return subprocess.run(cmd, **kwargs)


def decode(b):
    """把子进程输出统一成字符串（adb 输出可能是本地代码页）。"""
    if not b:
        return ''
    for enc in ('utf-8', 'mbcs', 'latin-1'):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode('utf-8', 'replace')


DEV = '127.0.0.1:16384'          # 默认设备（实例 0），实际由 Adb(device=...) 覆盖
BASE_PORT = 16384                # 实例 0 的 adb 端口
PORT_STEP = 32                   # 每多开一个实例端口 +32
TARGET = '/vendor/lib64/hw/android.hardware.sensors@2.1-impl.kona.so'
HAL_SVC = 'vendor.sensors-hal-2-1-multihal'
TMP = '/data/local/tmp'
PIDFILE = f'{TMP}/inject.pid'
BUF_OFF = 0xFCF0

IDLE_FRAME = (b'     0.100:     9.800:     0.200'
              b'      0.000:     0.000:     0.000'
              b'   -174.300:   -63.600:     0.000')

# 待补丁的三处 lea 与目标缓冲区（33 字节一块，紧邻）
BRANCHES = {
    'acceleration': (0x9240, 0xFCF0),
    'gyroscope': (0x936F, 0xFD11),
    'orientation': (0x93C1, 0xFD32),
}
PT_GNU_RELRO = 0x6474E552
ORIG_LEA = bytes([0x48, 0x8D, 0xBD])
NEW_LEA = bytes([0x48, 0x8D, 0x3D])

# ring 记录: +0x00 时间戳, +0x08 meta=(type<<32)|slot, +0x10 xyz
RING_META = {0x68: (4, 1), 0xC8: (2, 2), 0x128: (3, 3)}


class Adb:
    def __init__(self, path, device=DEV, log=None):
        self.path = path
        self.device = device
        self.log = log or (lambda *_: None)

    def run(self, *args, timeout=30, binary=False):
        if not os.path.isfile(self.path):
            raise RuntimeError(f'找不到 adb: {self.path}')
        r = quiet_run([self.path, *[str(a) for a in args]], timeout=timeout)
        return r.stdout if binary else decode(r.stdout + r.stderr)

    def sh(self, cmd, timeout=30):
        return self.run('-s', self.device, 'shell', cmd, timeout=timeout)

    def push_script(self, script, remote=None):
        """只把脚本推到设备，不执行（常驻脚本必须用后台方式启动）。"""
        remote = remote or f'{TMP}/_s.sh'
        with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False,
                                         encoding='utf-8', newline='\n') as f:
            f.write(script)
            local = f.name
        try:
            self.run('-s', self.device, 'push', local, remote)
        finally:
            os.unlink(local)
        return remote

    def run_script(self, script, timeout=120):
        """把脚本推到设备并同步执行，返回输出。"""
        with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False,
                                         encoding='utf-8', newline='\n') as f:
            f.write(script)
            local = f.name
        try:
            self.run('-s', self.device, 'push', local, f'{TMP}/_s.sh')
            return self.sh(f'sh {TMP}/_s.sh', timeout=timeout)
        finally:
            os.unlink(local)

    def push_bytes(self, data, remote):
        with tempfile.NamedTemporaryFile('wb', suffix='.bin', delete=False) as f:
            f.write(data)
            local = f.name
        try:
            self.run('-s', self.device, 'push', local, remote)
        finally:
            os.unlink(local)

    def connect(self):
        """连接并确保拿到 root；adb root 会重启 adbd，需重连后复查。"""
        self.run('connect', self.device)
        self.run('-s', self.device, 'root')
        time.sleep(1.0)
        for _ in range(4):
            if self.sh('id -u').strip() == '0':
                return True
            # adbd 重启后连接标识会失效，重连再试
            self.run('connect', self.device)
            time.sleep(1.2)
        return self.sh('id -u').strip() == '0'

    def hal_pid(self):
        out = self.sh(f'pidof {HAL_SVC.replace("-service", "-service")}')
        pid = self.sh('pidof android.hardware.sensors@2.1-service.multihal').strip()
        return pid

    def buf_addr(self):
        pid = self.hal_pid()
        if not pid:
            return None, None
        maps = self.sh(f'grep -F kona.so /proc/{pid}/maps')
        for line in maps.splitlines():
            m = re.match(r'([0-9a-f]+)-', line)
            if m:
                return int(m.group(1), 16) + BUF_OFF, pid
        return None, pid

    def find_ring(self, pid):
        """扫描 HAL 可写段，按 ring 记录头结构定位。"""
        regions = []
        for line in self.sh(f'cat /proc/{pid}/maps').splitlines():
            m = re.match(r'([0-9a-f]+)-([0-9a-f]+) rw-p 00000000 00:00 0', line)
            if not m:
                continue
            s, e = int(m.group(1), 16), int(m.group(2), 16)
            if 4096 <= e - s <= 2 << 20:
                regions.append((s, e))
        if not regions:
            return None

        script = ['#!/system/bin/sh']
        for s, e in regions:
            script.append(f'dd if=/proc/{pid}/mem bs=4096 skip={s // 4096} '
                          f'count={(e - s) // 4096} 2>/dev/null')
        # 不经过 shell：推脚本到设备后用 exec-out 直取原始字节。
        # shell=True 会额外拉起 cmd.exe，多一次黑窗闪烁。
        self.push_script('\n'.join(script), f'{TMP}/_dump.sh')
        blob = self.run('-s', self.device, 'exec-out', 'sh', f'{TMP}/_dump.sh',
                        timeout=60, binary=True)

        if not blob:
            return None
        pat = struct.pack('<Q', (1 << 32) | 0)
        others = {d: struct.pack('<Q', (t << 32) | s) for d, (t, s) in RING_META.items()}
        off = 0
        while True:
            i = blob.find(pat, off)
            if i < 0:
                return None
            off = i + 1
            rec = i - 8
            if rec < 0 or rec + 0x130 > len(blob):
                continue
            if not any(blob[rec + d:rec + d + 8] == p for d, p in others.items()):
                continue
            acc = 0
            for start, size in [(s, e - s) for s, e in regions]:
                if acc <= rec < acc + size:
                    return start + rec - acc
                acc += size
            return None


# ---------------------------------------------------------------- HAL 补丁
def make_patched_so(src_bytes):
    """关掉 RELRO 并把三处解析源指向可写缓冲，返回新 .so 字节。"""
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError:
        raise RuntimeError('需要 pyelftools：pip install pyelftools')

    import io
    elf = ELFFile(io.BytesIO(src_bytes))
    text = elf.get_section_by_name('.text')
    taddr, toff = text['sh_addr'], text['sh_offset']

    raw = bytearray(src_bytes)
    e_phoff = struct.unpack_from('<Q', raw, 0x20)[0]
    e_phentsize = struct.unpack_from('<H', raw, 0x36)[0]
    e_phnum = struct.unpack_from('<H', raw, 0x38)[0]
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if struct.unpack_from('<I', raw, off)[0] == PT_GNU_RELRO:
            struct.pack_into('<I', raw, off, 0)
            break
    else:
        raise RuntimeError('PT_GNU_RELRO 未找到')

    for name, (lea, buf) in BRANCHES.items():
        co = toff + (lea - taddr)
        if bytes(raw[co:co + 3]) != ORIG_LEA:
            raise RuntimeError(f'{name}: 0x{lea:x} 处字节不符，HAL 版本不匹配')
        raw[co:co + 7] = NEW_LEA + struct.pack('<i', buf - (lea + 7))
    return bytes(raw)


# ---------------------------------------------------------------- 波形
GRAVITY = 9.81
G0 = (0.1, 9.8, 0.2)
AZ0, PITCH0, ROLL0 = -174.3, -63.6, 0.0


def _rot(v, deg, axis):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    x, y, z = v
    return (x, y * c - z * s, y * s + z * c) if axis == 'x' else (x * c - y * s, x * s + y * c, z)


class _Rng:
    def __init__(self, seed=12345):
        self.s = seed & 0xFFFFFFFF or 1

    def gauss(self, sigma):
        u = []
        for _ in range(2):
            x = self.s
            x ^= (x << 13) & 0xFFFFFFFF
            x ^= x >> 17
            x ^= (x << 5) & 0xFFFFFFFF
            self.s = x & 0xFFFFFFFF
            u.append(max(self.s / 0xFFFFFFFF, 1e-9))
        return sigma * math.sqrt(-2 * math.log(u[0])) * math.cos(2 * math.pi * u[1])


def generate(fps, duration, cadence, seed=12345,
             pitch_amp=8.0, roll_amp=5.0, vert_amp=4.5,
             fore_amp=2.2, lat_amp=1.3, noise=0.12):
    """生成两套帧表：ring（36B/帧，二进制）与 text（99B/帧）。"""
    nrm = math.sqrt(sum(c * c for c in G0))
    g0 = tuple(c * GRAVITY / nrm for c in G0)
    w, ws = 2 * math.pi * cadence / 60.0, math.pi * cadence / 60.0
    rng = _Rng(seed)
    ring, text = bytearray(), bytearray()

    for i in range(int(round(duration * fps))):
        t = i / fps
        ph, phs = w * t, ws * t
        pitch = pitch_amp * math.sin(ph + 0.6) + 1.2 * math.sin(0.25 * ph)
        roll = roll_amp * math.sin(phs + 1.1) + 0.6 * math.sin(0.4 * phs)
        yaw = 2.5 * math.sin(2 * math.pi * t / 25.0)

        g = _rot(_rot(g0, pitch, 'x'), roll, 'z')
        lin = _rot(_rot((lat_amp * math.sin(phs + 1.0),
                         vert_amp * math.sin(ph) + 0.35 * vert_amp * math.sin(2 * ph + 0.7),
                         fore_amp * math.sin(ph + math.pi / 2)), pitch, 'x'), roll, 'z')
        acc = [g[k] + lin[k] + rng.gauss(noise) for k in range(3)]

        gyr = [math.radians(pitch_amp * w * math.cos(ph + 0.6)
                            + 1.2 * 0.25 * w * math.cos(0.25 * ph)) + rng.gauss(0.02),
               math.radians(roll_amp * ws * math.cos(phs + 1.1)
                            + 0.6 * 0.4 * ws * math.cos(0.4 * phs)) + rng.gauss(0.02),
               math.radians(2.5 * (2 * math.pi / 25) * math.cos(2 * math.pi * t / 25)) + rng.gauss(0.02)]
        ori = [AZ0 + yaw, PITCH0 + pitch, ROLL0 + roll]

        vals = acc + gyr + ori
        ring += struct.pack('<9f', *vals)
        text += b''.join(f'{v:10.3f}'.encode() + (b'\0' if k % 3 == 2 else b':')
                         for k, v in enumerate(vals))
    return bytes(ring), bytes(text)


def idle_text():
    return IDLE_FRAME


# ---------------------------------------------------------------- 多开实例
def find_manager(adb_path):
    """由 adb 路径反推 MuMuManager.exe（同目录），找不到返回空。"""
    if not adb_path:
        return ''
    p = os.path.join(os.path.dirname(os.path.abspath(adb_path)), 'MuMuManager.exe')
    return p if os.path.isfile(p) else ''


def list_instances(adb_path, timeout=20):
    """列出本机所有 MuMu 实例。

    返回 [{'index', 'name', 'device', 'started'}]，按 index 升序。
    优先问 MuMuManager；拿不到时退回「按端口规律探测」。
    """
    out = []
    mgr = find_manager(adb_path)
    if mgr:
        try:
            raw = quiet_run([mgr, 'info', '-v', 'all'], timeout=timeout)
            text = decode(raw.stdout)
            # 输出是 JSON，但前面可能混有杂字节，截取第一个 { 到最后一个 }
            s, e = text.find('{'), text.rfind('}')
            if s >= 0 and e > s:
                import json as _json
                data = _json.loads(text[s:e + 1])
                for key, info in sorted(data.items(), key=lambda kv: str(kv[0])):
                    if not isinstance(info, dict):
                        continue
                    port = info.get('adb_port')
                    if not port:
                        port = BASE_PORT + int(info.get('index', key)) * PORT_STEP
                    out.append({
                        'index': str(info.get('index', key)),
                        'name': info.get('name') or f'实例 {key}',
                        'device': f"{info.get('adb_host_ip', '127.0.0.1')}:{port}",
                        'started': bool(info.get('is_android_started')),
                    })
        except Exception:
            pass

    if not out:
        # 回退：按端口规律探测，只保留能连上的
        for i in range(8):
            dev = f'127.0.0.1:{BASE_PORT + i * PORT_STEP}'
            try:
                r = quiet_run([adb_path, 'connect', dev], timeout=8)
                txt = decode(r.stdout)
                started = 'connected' in txt and 'cannot' not in txt.lower()
            except Exception:
                started = False
            out.append({'index': str(i), 'name': f'实例 {i}',
                        'device': dev, 'started': started})

    return out


def running_instances(adb_path, timeout=20):
    """只返回已在运行（Android 已启动）的实例。"""
    return [x for x in list_instances(adb_path, timeout) if x['started']]


# ---------------------------------------------------------------- 会话
class Session:
    """针对单个模拟器实例的部署 + 注入会话。"""

    def __init__(self, adb_path, device=DEV, log=None, tag=''):
        self.device = device
        self.tag = tag
        self.adb = Adb(adb_path, device=device)
        base_log = log or (lambda *_: None)
        # 多实例时给每条日志加前缀，便于区分
        self.log = (lambda m: base_log(f'[{tag}] {m}')) if tag else base_log
        self.adb.log = self.log
        self.ring = None
        self.pid = None
        self.buf_addr = None

    def status(self):
        pid = self.adb.hal_pid()
        inj = self.adb.sh(f'cat {PIDFILE} 2>/dev/null').strip()
        alive = ''
        if inj:
            alive = self.adb.sh(f'kill -0 {inj} 2>/dev/null && echo alive || echo dead').strip()
        return {'pid': pid, 'injector': inj, 'injector_state': alive}

    def stop(self):
        self.adb.sh(f'kill $(cat {PIDFILE} 2>/dev/null) 2>/dev/null; rm -f {PIDFILE}')
        self.log('注入已停止')

    def restore(self):
        self.stop()
        self.adb.sh(f'stop {HAL_SVC}; sleep 1; umount {TARGET} 2>/dev/null; '
                    f'start {HAL_SVC}; sleep 2')
        self.log('已还原官方 HAL')

    def deploy(self, so_bytes):
        self.log('生成动态 HAL 补丁')
        patched = make_patched_so(so_bytes)
        self.adb.push_bytes(patched, f'{TMP}/hal_dyn.so')

        self.log('部署到 /vendor 并重启 HAL')
        self.adb.sh(f'kill $(cat {PIDFILE} 2>/dev/null) 2>/dev/null; rm -f {PIDFILE}; '
                    f'stop {HAL_SVC}; sleep 1; umount {TARGET} 2>/dev/null')
        r = self.adb.sh(f'mount --bind {TMP}/hal_dyn.so {TARGET}; echo rc=$?')
        if 'rc=0' not in r:
            raise RuntimeError(f'bind mount 失败: {r}')
        self.adb.sh(f'start {HAL_SVC}; sleep 2')

        buf, pid = self.adb.buf_addr()
        if not buf:
            raise RuntimeError('HAL 未启动')
        self.pid = pid
        self.adb.push_bytes(IDLE_FRAME, f'{TMP}/idle.bin')
        self.adb.sh(f'dd if={TMP}/idle.bin of=/proc/{pid}/mem bs=1 count=99 '
                    f'seek={buf} conv=notrunc 2>/dev/null')
        self.buf_addr = buf
        self.log(f'HAL pid={pid} 解析缓冲=0x{buf:X}')
        return buf

    def ensure_probe(self, apk_path):
        """确保设备上装了验证探针（ring 需要客户端订阅才会被填满）。"""
        if not apk_path or not os.path.isfile(apk_path):
            return False
        has = self.adb.sh('pm list packages com.probe.sensors')
        if 'com.probe.sensors' in has:
            return True
        self.log('安装验证探针')
        self.adb.run('-s', self.device, 'install', '-r', apk_path, timeout=120)
        return 'com.probe.sensors' in self.adb.sh('pm list packages com.probe.sensors')

    def locate_ring(self, attempts=5, wait=3, apk_path=None):
        self.ensure_probe(apk_path)
        for i in range(1, attempts + 1):
            self.log(f'定位环形缓冲（第 {i} 次）')
            self.adb.sh(f'am force-stop com.probe.sensors; '
                        f'am start -n com.probe.sensors/com.probe.ProbeActivity')
            time.sleep(wait)
            ring = self.adb.find_ring(self.pid)
            if ring:
                self.ring = ring
                self.log(f'ring=0x{ring:X}')
                return ring
        raise RuntimeError('未能定位环形缓冲（探针未运行或设备掉线）')

    def calibrate(self, target_fps=50, n=100):
        """测量单帧写入耗时，返回 (sleep秒, 实测fps)。"""
        ring = self.ring
        writes = '\n'.join(
            f'  dd if={TMP}/idle.bin bs=1 count=12 of=/proc/{self.pid}/mem '
            f'bs=1 seek={ring + off} conv=notrunc 2>/dev/null'
            for off in (0x10, 0x70, 0x130))

        def timed(extra):
            out = self.adb.run_script(
                '#!/system/bin/sh\nS=$(date +%s%N)\ni=0\n'
                f'while [ $i -lt {n} ]; do\n{writes}\n{extra}\n  i=$((i+1))\ndone\n'
                'E=$(date +%s%N)\necho "$S $E"\n')
            m = re.search(r'(\d{10,})\s+(\d{10,})', out)
            if not m:
                return None
            return (int(m.group(2)) - int(m.group(1))) / n / 1e9

        cost = timed('') or 0.015
        sleep_s = max(0.0, 1.0 / target_fps - cost)
        actual = timed(f'  sleep {round(sleep_s, 4)}')
        fps = round(1.0 / actual, 1) if actual else 0
        if not 5 <= fps <= 120:
            fps = 40
        self.log(f'单帧 {cost*1000:.1f}ms, sleep {sleep_s*1000:.1f}ms -> {fps}Hz')
        return sleep_s, fps

    def start_injection(self, ring_bytes, text_bytes, frames, sleep_s, loop=True):
        self.adb.push_bytes(ring_bytes, f'{TMP}/f_ring.bin')
        self.adb.push_bytes(text_bytes, f'{TMP}/f_text.bin')
        ring, pid = self.ring, self.pid
        buf = self.buf_addr            # 解析缓冲区地址（deploy 时已算好）
        script = f"""#!/system/bin/sh
P={pid}
A={ring + 0x10}
G={ring + 0x70}
O={ring + 0x130}
T={buf}
FRAMES={frames}
LOOP={1 if loop else 0}
SLEEP={round(sleep_s, 4)}

echo $$ > {PIDFILE}
i=0
while : ; do
  OFF=$((i*36))
  dd if={TMP}/f_ring.bin bs=1 skip=$OFF count=12 of=/proc/$P/mem bs=1 seek=$A conv=notrunc 2>/dev/null
  dd if={TMP}/f_ring.bin bs=1 skip=$((OFF+12)) count=12 of=/proc/$P/mem bs=1 seek=$G conv=notrunc 2>/dev/null
  dd if={TMP}/f_ring.bin bs=1 skip=$((OFF+24)) count=12 of=/proc/$P/mem bs=1 seek=$O conv=notrunc 2>/dev/null
  if [ $((i % 8)) -eq 0 ]; then
    dd if={TMP}/f_text.bin bs=1 skip=$((i*99)) count=99 of=/proc/$P/mem bs=1 seek=$T conv=notrunc 2>/dev/null
  fi
  i=$((i+1))
  if [ $i -ge $FRAMES ]; then
    if [ "$LOOP" = "1" ]; then i=0; else break; fi
  fi
  sleep $SLEEP
done
rm -f {PIDFILE}
"""
        self.adb.push_script(script, f'{TMP}/inject.sh')
        # 常驻脚本必须后台启动，且断开所有继承的 fd，否则 adb shell 不返回
        self.adb.sh(f'setsid sh {TMP}/inject.sh </dev/null >/dev/null 2>&1 & sleep 1')
        self.log(f'注入器已启动，{frames} 帧')


# ---------------------------------------------------------------- 批量控制
class Fleet:
    """对多个模拟器实例同时执行操作（部署、注入、停止、还原）。"""

    def __init__(self, adb_path, log=None):
        self.adb_path = adb_path
        self.log = log or (lambda *_: None)
        self.sessions = {}          # device -> Session

    def discover(self, only_running=True):
        """返回可用的实例列表 [{index,name,device,started}]。"""
        items = list_instances(self.adb_path)
        if only_running:
            items = [x for x in items if x['started']]
        if not items:
            # 兜底：至少保证默认设备在列
            items = [{'index': '0', 'name': '实例 0', 'device': DEV, 'started': True}]
        return items

    def _make(self, item):
        """同一设备复用同一个 Session，保住 ring/pid 等中间状态。"""
        dev = item['device']
        s = self.sessions.get(dev)
        if s is None:
            tag = f'实例{item["index"]}'
            s = Session(self.adb_path, device=dev, log=self.log, tag=tag)
            self.sessions[dev] = s
        return s

    def _each(self, items, fn, parallel=False):
        """对每个实例执行 fn(session, item)，收集结果。"""
        results = {}

        def one(item):
            s = self._make(item)
            self.sessions[item['device']] = s
            try:
                results[item['device']] = (True, fn(s, item))
            except Exception as e:
                results[item['device']] = (False, str(e))
                self.log(f'[实例{item["index"]}] ✗ {e}')

        if parallel and len(items) > 1:
            import threading
            threads = [threading.Thread(target=one, args=(it,)) for it in items]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        else:
            for it in items:
                one(it)
        return results

    def deploy_all(self, so_bytes, items=None, parallel=True):
        items = items or self.discover()

        def job(s, item):
            # 每个实例都独立确保已连接并取得 root
            if not s.adb.connect():
                raise RuntimeError('无法连接或未取得 root')
            return s.deploy(so_bytes)

        return self._each(items, job, parallel)

    def locate_rings(self, items=None, parallel=True, apk_path=None):
        items = items or self.discover()

        def job(s, item):
            if not s.adb.connect():
                raise RuntimeError('无法连接')
            return s.locate_ring(apk_path=apk_path)

        return self._each(items, job, parallel)

    def calibrate_all(self, items=None, target_fps=50, parallel=True):
        items = items or self.discover()

        def job(s, item):
            if not s.adb.connect():
                raise RuntimeError('无法连接')
            return s.calibrate(target_fps)

        return self._each(items, job, parallel)

    def start_all(self, ring_bytes, text_bytes, frames, sleep_map, loop=True,
                  items=None, parallel=True):
        """sleep_map: device -> sleep_s（各实例性能不同，分别校准过）。"""
        items = items or self.discover()

        def job(s, item):
            if not s.adb.connect():
                raise RuntimeError('无法连接')
            # 复用前面 deploy/locate 阶段缓存下来的 ring/pid，避免重扫
            sleep_s = sleep_map.get(item['device'], 0.008)
            s.start_injection(ring_bytes, text_bytes, frames, sleep_s, loop=loop)
            return sleep_s

        return self._each(items, job, parallel)

    def stop_all(self, items=None, parallel=True):
        items = items or self.discover()

        def job(s, item):
            if not s.adb.connect():
                raise RuntimeError('无法连接')
            s.stop()

        return self._each(items, job, parallel)

    def restore_all(self, items=None, parallel=True):
        items = items or self.discover()

        def job(s, item):
            if not s.adb.connect():
                raise RuntimeError('无法连接')
            s.restore()

        return self._each(items, job, parallel)
