# Shimadzu UV-Vis Automation

用于通过岛津 **LabSolutions UV-Vis 自动控制文本交换功能**执行测量的 Python 工具。

本项目不会直接访问仪器 USB、串口或底层驱动。控制链路是：

```text
Python -> SPC_CMD.txt -> LabSolutions UV-Vis -> Shimadzu UV-Vis
Python <- SPC_RES.txt <- LabSolutions UV-Vis
Python <- CSV/TXT/XLSX auto export <- LabSolutions UV-Vis
```

目前包含：

- 原子写入命令文件，避免 LabSolutions 读到半个文件
- 解析反馈并在 `Return != 0` 时明确报错
- Spectrum 模式的完整测量流程
- 等待 CSV/TXT/XLSX 导出完成并确认文件已稳定
- 通用命令发送入口，便于扩展 Quantitation、Photometric 和 Time Course
- 本地 LabSolutions 模拟器，可在不连接仪器时测试流程
- 零第三方运行时依赖

## 安装

需要 Python 3.11 或更高版本。

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item config.example.toml config.toml
```

根据仪器电脑上的实际目录修改 `config.toml`。

## 先用模拟器验证

打开第一个 PowerShell：

```powershell
labsolutions-simulator `
  --command-dir .\runtime\control `
  --export-dir .\runtime\export
```

打开第二个 PowerShell：

```powershell
shimadzu-uvvis `
  --command-dir .\runtime\control `
  ping

shimadzu-uvvis `
  --command-dir .\runtime\control `
  spectrum `
  --method C:\UVVis-Data\Parameter\growth_scan_300_900.vspm `
  --sample-name test_sample `
  --sample-id run_001 `
  --data-file C:\UVVis-Data\Data\run_001 `
  --export-dir .\runtime\export `
  --export-pattern "*_SIMULATED.csv"
```

模拟器生成的文件名和内容都带有 `SIMULATED` 标记，不能当作真实测量数据。

## 连接真实 LabSolutions

1. 在 LabSolutions UV-Vis 中保存测量参数文件，例如 `growth_scan_300_900.vspm`。
2. 在 LabSolutions 中预设测量后自动导出 CSV、TXT 或 Excel，并固定导出目录。
3. 在 `Tools -> Customize -> Automatic Control` 中确认命令接收目录，默认通常是 `C:\UVVisControl`。
4. 打开 `Instrument -> Automatic Control`，保持自动控制窗口运行。
5. 先执行 `ping`，确认返回 `Return=0`。

```powershell
shimadzu-uvvis --config config.toml ping
```

执行一次 Spectrum 测量：

```powershell
shimadzu-uvvis `
  --config config.toml `
  spectrum `
  --connect `
  --method C:\UVVis-Data\Parameter\growth_scan_300_900.vspm `
  --sample-name Au_growth_01 `
  --sample-id run_20260711_001 `
  --data-file C:\UVVis-Data\Data\run_20260711_001 `
  --correction auto
```

如果仪器已经连接，可省略 `--connect`。只有确实希望测量后断开仪器时才加入 `--disconnect`。

发送手册中的任意单条命令：

```powershell
shimadzu-uvvis --config config.toml send 12 CellPosition=3
```

## 安全边界

这是实验室自动化软件，不替代仪器联锁、操作规程或岛津工程师确认。首次连接真实仪器时，应使用空白样品并由熟悉设备的人员现场观察；在确认基线、附件位置、方法文件和导出格式前，不要无人值守运行。

详细设置、命令映射和验收步骤见 [docs/labsolutions-operation.md](docs/labsolutions-operation.md)。

## 开发与测试

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## 状态

当前版本为 `0.1.0-alpha`。核心文件交换流程已有自动测试，但仍需在具体 LabSolutions 版本和具体 UV-Vis 型号上完成现场验收。
