# Shimadzu UV-Vis Automation

通过岛津 **LabSolutions UV-Vis 自动控制文本交换功能**执行可审计的 Spectrum 测量。

本项目不直接访问仪器 USB、串口或底层驱动。实际控制链路是：

```text
Python -> SPC_CMD.txt -> LabSolutions UV-Vis -> Shimadzu UV-Vis
Python <- SPC_RES.txt <- LabSolutions UV-Vis
Python <- CSV/TXT/XLSX automatic export <- LabSolutions UV-Vis
```

当前版本已具备控制电脑现场测试所需的基础能力：

- Windows 一键环境与配置脚本，不需要联网安装 Python 包
- LabSolutions 目录、方法文件、数据目录和写权限诊断
- 原子命令写入和跨进程互斥，防止两个程序同时控制仪器
- 默认先显示完整命令计划，只有显式确认才执行测量
- 每条命令的 JSON 审计记录，以及每次测量的 run manifest
- 自动等待 CSV/TXT/XLSX 输出稳定，并记录大小和 SHA-256
- LabSolutions 模拟器和一键端到端验收脚本
- 真实通道分级测试：文件系统诊断、`Hello`、首个样品测量

## 控制电脑快速开始

控制电脑需要 Windows PowerShell 5.1 或更高版本，以及 Python 3.11 或更高版本。

除了 `git clone`，也可以在开发电脑提交代码后运行 `scripts\build-control-pc-bundle.ps1`，生成不包含 `.git`、`.venv`、运行数据和本机配置的干净 ZIP。

### 1. 获取仓库并初始化

```powershell
git clone https://github.com/chemyibinjiang/shimadzu-uvvis-automation.git
cd shimadzu-uvvis-automation

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\setup-control-pc.ps1 `
  -MethodFile C:\UVVis-Data\Parameter\growth_scan_300_900.vspm `
  -ScanStartNm 300 -ScanStopNm 900 -ScanStepNm 1
```

该脚本会：

- 创建本地 `.venv`，但不下载第三方依赖
- 创建命令、数据、导出和审计目录
- 生成被 Git 忽略的 `control-pc.toml`
- 保留已有配置，除非加入 `-Force`

### 2. 先跑完整模拟验收

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test-simulator.ps1
```

成功标志为：

```text
SIMULATOR ACCEPTANCE PASSED
```

验收报告位于 `runtime\simulator-acceptance\<时间>\reports`。模拟数据、方法和结果均带有 `SIMULATED` 标记，不能作为实验数据。

### 3. 配置 LabSolutions

1. 在 LabSolutions UV-Vis 中保存并人工验证 Spectrum 参数文件 `.vspm`。
2. 在 `Tools -> Customize -> Automatic Control` 中设置命令接收目录。手册默认值为 `C:\UVVisControl`。
3. 在 LabSolutions 中设置测量后自动导出 CSV、TXT 或 Excel 到 `C:\UVVis-Data\Export`。
4. 最好让导出文件名以 `SampleID` 开头，例如 `validation_20260711_001.csv`。
5. 打开 `Instrument -> Automatic Control`，直到窗口显示等待命令。

### 4. 检查真实控制电脑，但不发送命令

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test-live.ps1
```

该步骤只创建无害的探针文件来验证读写权限，不会发送 LabSolutions 命令。

### 5. 测试 LabSolutions Hello

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test-live.ps1 -Ping
```

这只发送 `Command=0`，不会连接仪器、移动附件、校正或测量。

### 6. 首次测量先查看计划

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run-test-measurement.ps1 `
  -SampleName validation_blank
```

确认命令计划、当前样品池、空白/样品和 LabSolutions 方法均正确后，再显式执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run-test-measurement.ps1 `
  -SampleName validation_blank `
  -Execute
```

仪器尚未连接时加入 `-Connect`。只有空白样品已经正确放置并且确实需要校正时才加入 `-AutoCorrection`。

完整现场步骤见 [控制电脑验收手册](docs/control-pc-acceptance.md)。

波长范围、旧式 `start/stop/step` 兼容方式和多波长边界见 [波长控制说明](docs/wavelength-control.md)。

例如，使用已登记方法匹配旧控制器参数，并记录三个目标波长：

```powershell
.\scripts\uvvis.ps1 spectrum `
  --start 300 --stop 900 --step 1 `
  --wavelengths 450 520 650 `
  --sample-name validation_sample
```

该命令默认只显示计划；确认后再加入 `--execute`。

首次测量包装脚本也支持配置名称和多目标点：

```powershell
.\scripts\run-test-measurement.ps1 `
  -SampleName validation_sample `
  -Profile default `
  -WavelengthsNm "450,520,650"
```

## 安全默认值

首个测试脚本会强制采用：

- `MeasurementMode=2`：多联池时只测当前样品池
- `Discharge=OFF`：不请求抽吸附件在测量后排出
- `correction=none`：不自动执行基线校正或调零
- `disconnect=false`：测量后不主动断开仪器
- ASCII 样品 ID 和路径

岛津手册说明，`MeasurementMode=1` 会测量全部已配置样品池；使用抽吸附件时，如果省略 `Discharge`，软件可能按启用处理。因此本项目始终显式发送安全值。

## 日志与结果

默认目录：

```text
C:\UVVis-Automation\Logs
```

每次执行会产生：

- `<日期>\..._cmd<N>_<request-id>.json`：精确命令、反馈、耗时和异常
- `runs\<SampleID>.json`：整次测量的命令结果、数据路径和导出文件 SHA-256
- LabSolutions 原始 Spectrum 数据文件：`C:\UVVis-Data\Data\<SampleID>.vspd`
- LabSolutions 自动导出的 CSV/TXT/XLSX

如果发生超时，先检查审计文件中的 `command_written` 和 `command_file_exists_at_record_time`，不要立即重发测量命令。

每条命令发送前还会建立恢复标记，只有收到匹配的 `SPC_RES.txt` 才自动清除。超时后可先查看：

```powershell
.\scripts\uvvis.ps1 recover
```

确认 LabSolutions 已结束当前动作并检查过反馈后，先查看清除计划，再显式确认：

```powershell
.\scripts\uvvis.ps1 recover --clear
.\scripts\uvvis.ps1 recover --clear --execute
```

只有在没有匹配反馈、但操作人员已经从仪器和 LabSolutions 确认最终状态时，才考虑额外使用 `--force`。

## 命令行

控制电脑上推荐通过包装脚本运行，它会自动使用 `.venv` 和 `control-pc.toml`：

```powershell
.\scripts\uvvis.ps1 doctor --write-check
.\scripts\uvvis.ps1 ping
```

任意非零通用命令默认只显示计划：

```powershell
.\scripts\uvvis.ps1 send 12 CellPosition=3
```

确认后才真正发送：

```powershell
.\scripts\uvvis.ps1 send 12 CellPosition=3 --execute
```

通用命令可能移动附件或改变仪器状态，应以对应版本的岛津自动控制手册为准。

## 开发测试

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

核心运行时没有第三方 Python 依赖。GitHub Actions 在 Windows 和 Python 3.11/3.12 上运行测试。

## 项目边界

这是 LabSolutions 上层文本交换客户端，不是岛津底层驱动，也不替代仪器联锁、SOP 或现场工程师确认。当前实现面向单机 LabSolutions Spectrum 测量；DB/CS 文件标识、多联池批量测量、抽吸/清洗附件和无人值守运行需要单独验收。
