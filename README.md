# MuMu 拟真跑步数据注入器

让 MuMu 模拟器持续输出**拟真跑步**的加速度、陀螺仪、姿态角读数——不是常量，
而是每秒变化 40 多次的连续运动波形。

---

## 使用

### 方式一：单文件 EXE（推荐，目标机器无需装 Python）

双击 `dist\MuMu跑步注入器.exe`。

首次启动会把内嵌运行时解压到 `%TEMP%\MuMuRunner_<hash>`（约 5 秒），之后秒开。

> **注意**：EXE 由系统自带 `csc.exe` 把 Python 运行时与脚本打包而成。
> 若杀毒软件报未知程序，加白名单即可；也可改用下面的 `.pyz` 形式。

### 方式二：单文件 .pyz（需 Python 3）

双击 `run.cmd`，或直接：

```powershell
python dist\mumu_runner.pyz
```

`.pyz` 只有 26 KB，标准库 `zipapp` 打包，源码可直接审阅。

### 界面

![界面](screenshot.png)

| 区域 | 说明 |
|---|---|
| **MuMu 安装目录** | **自动探测**（注册表 → 常见安装位置 → PATH 上的 adb 反推），也可点「浏览」手动指定；记住到 `config.json` |
| **目标实例** | 默认「全部运行中的实例」，也可只对某一个实例生效；点「刷新」重新扫描 |
| **时长（分钟）** | 1–720，默认 10 |
| **步频（步/分）** | 60–260，默认 174；提示：慢跑 150-165，正常 170-180，快跑 185+ |
| **▶ 启动** | 对所有选中实例依次部署并注入，运行中按钮置灰 |
| **■ 停止** | 停止注入，数值冻结 |
| **↺ 重置** | 二次确认后停止并还原官方 HAL |
| **日志** | 按 `[实例N]` 前缀区分各实例的进度 |

### 多开支持

MuMu 多开时每个实例是独立的 Android 环境，端口按规律递增（实例 0 = `16384`，
之后每个 +32）。程序会：

1. 调 `MuMuManager.exe info -v all` 枚举全部实例（拿不到时按端口规律探测）；
2. 只对**运行中**的实例操作，未启动的会标注 `[未启动]`；
3. 每个实例独立部署补丁、独立定位环形缓冲、独立校准帧率
   （不同实例性能不同，实测 39.8Hz 与 41.0Hz）；
4. 用同一份波形同时喂给所有实例，日志按 `[实例N]` 前缀区分。

多个实例的日志会交错，但每条都带实例前缀，便于定位问题。

### 多开端口规律

MuMu 多开时每个实例是完整的独立 Android 环境，adb 端口按固定步长递增：

| 实例 | adb 端口 |
|---|---|
| 0 | 16384 |
| 1 | 16416 |
| 2 | 16448 |
| N | 16384 + N × 32 |

程序优先调 `MuMuManager.exe info -v all` 直接读每个实例的 `adb_port`
（最可靠，也不依赖这个规律），读不到时才按上表逐端口探测。

---

## 原理

### 为什么必须写环形缓冲

1. HAL 从 `/dev/vaddress` 读宿主推送的文本行，`sscanf` 解析后写入**事件环形缓冲**；
2. 宿主只在数值变化时才推送——实测加速度约 1 次/秒；
3. App 收到的 50Hz 事件其实是 HAL 的 batch 线程从 ring **重发**的同一份数据。

所以只改解析源，App 每秒才跳一帧。真正要写的是 ring：

| 偏移 | 内容 |
|---|---|
| `+0x00` | uint64 时间戳 |
| `+0x08` | uint64 meta = `(类型 << 32) \| slot` |
| `+0x10` | float x, y, z ← **写这里** |

槽位：`0=accel`、`1=gyro`、`3=orientation`。ring 在堆上，HAL 每次重启地址都变，
所以部署时用 meta 结构（`0x0000000100000000` 且副槽命中）扫描定位，失败会重试。

### HAL 补丁

1. `PT_GNU_RELRO → PT_NULL`：让 RW 段尾部 `0xfcf0` 的匿名页保持可写
   （否则 bionic 把整页 mprotect 成只读，外部写不进去）；
2. 三条 `lea rdi,[rbp+disp]` → `lea rdi,[rip+disp]`：把 accel/gyro/orientation 的
   解析源指向 `0xfcf0` 起的三块 33 字节缓冲。

注入时每帧写 3 处 ring，每 8 帧刷新一次解析缓冲——这样宿主推送触发重新解析时，
写回 ring 的仍是同一帧，不会跳变。

### 波形

一套姿态/步态模型同时驱动三路，因此彼此自洽：

- **加速度** = 重力按当前姿态旋转 + 垂直（步频主振荡 + 二次谐波）/前后/侧向分量 + 噪声；
- **陀螺仪** = 姿态角对时间求导（不是独立随机数，积分回去能对应姿态变化）；
- **姿态角** = 绕基准姿态（`-174.3 / -63.6 / 0`）摆动，横摆取步幅频率，方位角带 25 秒慢漂移。

---

## 源码结构

| 文件 | 作用 |
|---|---|
| `app.py` | tkinter 界面与交互（含实例选择、自动探测 MuMu 位置） |
| `core.py` | `Adb`（按设备绑定）、`Session`（单实例会话）、`Fleet`（多实例批量）、HAL 补丁、环形缓冲定位、波形生成 |
| `build.py` | 打包：收集运行时 → 压 payload → csc 编译 EXE → 同时产出 pyz |
| `launcher.cs` | EXE 启动器：从自身末尾读取 payload，解压后拉起 pythonw |
| `impl_kona.so` | 官方原始 HAL（md5 `0bc7861a…`，补丁基准，勿改） |
| `probe/probe.apk` | 验证探针，打印各传感器原始值（缺探针的实例会自动安装） |
| `run.cmd` | 便捷启动器（优先 pyz，回退源码） |
| `dist/MuMu跑步注入器.exe` | 单文件 EXE（13.5 MB，自带运行时） |
| `dist/mumu_runner.pyz` | 单文件 pyz（30 KB，需 Python） |

核心对象的职责边界：

- `Adb(path, device=...)` —— 绑到一个设备，所有命令都带 `-s <device>`
- `Session(adb_path, device=..., tag='实例N')` —— 单实例的完整流程，日志自动加前缀
- `Fleet(adb_path)` —— 枚举实例并批量调度，内部按设备复用 `Session`
  以保住 ring/pid 等中间状态

### 重新打包

```powershell
python build.py              # 产出 EXE + pyz
python build.py --pyz-only   # 只产出 29KB 的 .pyz（无需 .NET）
```

打包要点（都是踩过的坑）：

- **payload 不嵌入 .NET 资源**，而是追加在 exe 末尾（`[exe][zip][8B长度][8B魔数]`）。
  14 MB 资源会让 `winexe` 在 `Main` 之前就加载失败。
- **不用 `System.IO.Compression`**：`winexe` 加该引用后 `Main` 直接不执行。
  改用 `Shell.Application` COM 解压，零程序集依赖。
- Python 运行时用 `._pth` 显式声明 `Lib` 与 `..\app` 搜索路径；
  `._pth` 存在时会忽略 `PYTHONPATH`，所以 app 目录必须写进去。

---

## 验证

```powershell
adb -s 127.0.0.1:16384 install -r probe\probe.apk
adb -s 127.0.0.1:16384 shell am start -n com.probe.sensors/com.probe.ProbeActivity
adb -s 127.0.0.1:16384 logcat -s SensorProbe:I
```

判断标准：不同取值数应接近样本数（每帧都在变）、`|a|` 在 4–15 m/s² 起伏。

实测（步频设定 180）：

```
ACCEL 事件 166，不同取值 125；GYRO 127；ORIENT 127
ACCEL 0.385 10.751 3.312 → 0.182 13.338 3.748
```

---

## 已知边界

| 限制 | 说明 |
|---|---|
| 陀螺仪零阶保持 | 注入 ~37Hz，宿主按 204Hz 推送 → 每值重复约 5 次，回放类检测可发现 |
| 时间戳未更新 | ring 里的时间戳沿用宿主写入值 |
| 融合传感器间接跟随 | `GRAVITY`/`ROT_VEC` 由 SensorService 用 accel+gyro+mag 融合，大体自洽但非精确一致 |
| 重启失效 | bind mount 不持久，模拟器重启后需重新点「启动」 |
| 需客户端订阅 | 扫描前要先喂一帧解析缓冲并启动探针，否则 ring 是空的（程序已内置重试） |

这些说明它的定位：**用来测「拟真度检测」够不够灵敏**，而不是做到无懈可击。

### 版本对应

补丁基于 `impl_kona.so` md5 = `0bc7861ae7373a7f24a750f505247a46`。
MuMu 升级后若 HAL 变化，`core.make_patched_so` 会校验原始字节并报错，
此时需重新反汇编定位三处 `lea rdi`（当前地址 `0x9240` / `0x936F` / `0x93C1`）。

---

## 清理

- 停止注入并还原：界面点「重置」
- 删除临时解压：`rmdir /s %TEMP%\MuMuRunner_*`
- 卸载探针：`adb -s 127.0.0.1:16384 uninstall com.probe.sensors`
