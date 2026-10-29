using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

// 单文件启动器。
//
// 两个关键设计约束（都踩过坑）：
//  1. payload.zip 不嵌入为 .NET 资源，而是追加在 exe 末尾（overlay）：
//     [exe][payload.zip][8B 长度][8B 魔数]。14MB 资源会让 winexe 提前退出。
//  2. 不用 System.IO.Compression -- winexe 加上该引用后 Main 根本不会执行。
//     改用 Shell.Application COM 解压，零程序集依赖。
static class Launcher
{
    const string AppScript = "app.py";
    const long Magic = 0x4D554D555A49505AL;   // "MUMUZIP"

    static string LogPath = Path.Combine(Path.GetTempPath(), "mumu_runner_launcher.log");

    static void Log(string msg)
    {
        try { File.AppendAllText(LogPath, DateTime.Now.ToString("HH:mm:ss.fff ") + msg + "\r\n"); }
        catch { }
    }

    [STAThread]
    static int Main(string[] args)
    {
        Log("=== start ===");
        try
        {
            string self = Assembly.GetExecutingAssembly().Location;
            Log("self=" + self);

            long zipLen, zipOff;
            if (!ReadOverlay(self, out zipOff, out zipLen))
                throw new Exception("未找到内嵌数据（overlay 缺失）");
            Log("overlay offset=" + zipOff + " len=" + zipLen);

            string work = Path.Combine(Path.GetTempPath(), "MuMuRunner_" + zipLen.ToString("X"));
            string marker = Path.Combine(work, ".ready");
            Log("work=" + work);

            if (!File.Exists(marker))
            {
                Log("extracting...");
                Directory.CreateDirectory(work);
                ExtractOverlay(self, zipOff, zipLen, work);
                File.WriteAllText(marker, DateTime.Now.ToString("o"));
                Log("extracted");
            }
            else Log("cached");

            return Launch(work, args);
        }
        catch (Exception ex)
        {
            Log("FAILED: " + ex);
            try
            {
                MessageBox.Show("启动失败:\n\n" + ex.Message + "\n\n日志: " + LogPath,
                    "MuMu 跑步注入器", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            catch { }
            return 1;
        }
    }

    // 文件尾部 16 字节 = zip 长度(8) + 魔数(8)
    static bool ReadOverlay(string path, out long offset, out long length)
    {
        offset = length = 0;
        using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read))
        {
            if (fs.Length < 16) return false;
            fs.Seek(-16, SeekOrigin.End);
            var tail = new byte[16];
            if (fs.Read(tail, 0, 16) != 16) return false;
            if (BitConverter.ToInt64(tail, 8) != Magic) return false;
            length = BitConverter.ToInt64(tail, 0);
            offset = fs.Length - 16 - length;
            return offset >= 0 && length > 0;
        }
    }

    // 先把自己尾部那段 zip 单独写成临时文件，再用 Shell COM 解压到目标目录
    static void ExtractOverlay(string path, long offset, long length, string dest)
    {
        string zipPath = Path.Combine(Path.GetTempPath(), "MuMuRunner_payload.zip");
        using (var src = new FileStream(path, FileMode.Open, FileAccess.Read))
        using (var dst = new FileStream(zipPath, FileMode.Create, FileAccess.Write))
        {
            src.Seek(offset, SeekOrigin.Begin);
            var buf = new byte[81920];
            long left = length;
            while (left > 0)
            {
                int n = src.Read(buf, 0, (int)Math.Min(buf.Length, left));
                if (n <= 0) throw new Exception("读取内嵌数据不完整");
                dst.Write(buf, 0, n);
                left -= n;
            }
        }
        Log("payload written to " + zipPath);

        var shellType = Type.GetTypeFromProgID("Shell.Application");
        if (shellType == null) throw new Exception("无法创建 Shell.Application");
        object shell = Activator.CreateInstance(shellType);
        object zipFolder = shellType.InvokeMember("NameSpace", BindingFlags.InvokeMethod, null, shell,
                                                  new object[] { zipPath });
        if (zipFolder == null) throw new Exception("无法打开 payload（zip 可能损坏）");
        object destFolder = shellType.InvokeMember("NameSpace", BindingFlags.InvokeMethod, null, shell,
                                                   new object[] { dest });
        if (destFolder == null) throw new Exception("无法打开目标目录: " + dest);

        object items = zipFolder.GetType().InvokeMember("Items", BindingFlags.InvokeMethod, null,
                                                        zipFolder, null);
        destFolder.GetType().InvokeMember("CopyHere", BindingFlags.InvokeMethod, null,
                                          destFolder, new object[] { items, 0x14 });
        Log("shell copyhere done");

        // CopyHere 是异步的，等文件落盘
        string probe = Path.Combine(dest, "app", AppScript);
        for (int i = 0; i < 120 && !File.Exists(probe); i++) System.Threading.Thread.Sleep(250);
        if (!File.Exists(probe)) throw new Exception("解压未完成: " + probe);

        try { File.Delete(zipPath); } catch { }
    }

    static int Launch(string work, string[] args)
    {
        string exe = Path.Combine(work, "runtime", "pythonw.exe");
        if (!File.Exists(exe)) exe = Path.Combine(work, "runtime", "python.exe");
        if (!File.Exists(exe)) throw new Exception("运行时缺失: " + exe);

        string script = Path.Combine(work, "app", AppScript);
        if (!File.Exists(script)) throw new Exception("程序脚本缺失: " + script);

        var psi = new ProcessStartInfo
        {
            FileName = exe,
            Arguments = '"' + script + '"' + (args.Length > 0 ? " " + String.Join(" ", args) : ""),
            WorkingDirectory = Path.Combine(work, "app"),
            UseShellExecute = false,
            CreateNoWindow = true
        };
        psi.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
        Process.Start(psi);
        Log("child started");
        return 0;
    }
}
