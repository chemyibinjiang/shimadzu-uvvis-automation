# Shimadzu UV-Vis Automation

通过岛津 **LabSolutions UV-Vis 自动控制文本交换功能**执行可审计的 Spectrum 测量和重复完整光谱采集。

本项目不直接访问仪器 USB、串口或底层驱动。LabSolutions 负责连接仪器、执行已保存的方法、保存原始数据和自动导出结果；本项目负责命令调度、安全校验、结果关联和审计。

```text
Python -> SPC_CMD.txt -> LabSolutions UV-Vis -> Shimadzu UV-Vis
Python <- SPC_RES.txt <- LabSolutions UV-Vis
Python <- CSV/TXT/XLSX automatic export <- LabSolutions UV-Vis
```

> 当前代码、单元测试和文件交换模拟器已经通过验收。真实岛津仪器上的方法参数、附件行为、扫描耗时和自动导出仍需按本文的分级流程在控制电脑上验证。

## 能力与边界

| 功能 | 当前状态 | 说明 |
| --- | --- | --- |
| 单次完整 Spectrum | 已实现 | 加载已验证的 `.vspm`，设置样品信息，执行 `Command=111` |
| 重复完整 Spectrum | 已实现 | 使用单调时钟按 `Command=111` start-to-start 时间间隔调度 |
| `start/stop/step` 兼容入口 | 已实现 | 只匹配已登记且参数完全一致的 `.vspm`，不会现场生成方法 |
| 多个目标波长 | 已实现为校验和元数据 | 完整光谱仍会采集；`--wavelengths` 不代表离散多波长测量 |
| Photometric 离散多波长 | 尚未开放 | 需要在真实控制电脑上验收 `.vphm/.vphd` 和导出结构 |
| Time Course 固定波长时间曲线 | 尚未开放 | 时间间隔和总时长需预先保存在 `.vtcm` 中 |
| USB、串口或底层仪器 API | 不提供 | 当前岛津集成边界是 LabSolutions 上层文本交换 |

核心安全能力：

- 默认只显示命令计划，加入 `--execute` 或 `-Execute` 才测量
- 原子命令写入和完整工作流跨进程互斥
- 超时后建立恢复标记，禁止状态不明时自动重发
- 每条命令、每次测量和每个序列均生成 JSON 审计记录
- 等待 CSV/TXT/XLSX 文件稳定后再计算大小和 SHA-256
- 每次序列采集使用唯一 SampleID、`.vspd` 路径和导出匹配模式
- 前一次扫描或导出超时后，在下一次 `Command=111` 之前停止

## 波长与时间的含义

### 连续光谱内部

起始波长、终止波长、数据间隔、扫描速度和响应设置位于 LabSolutions `.vspm` 方法中。`Command=111` 不提供这些运行时参数，也没有旧底层控制器中逐波长 `settle_time` 的等价参数。

本项目只登记经过人工核对的方法元数据：

```toml
[scan_profiles.default]
method_file = "C:\\UVVis-Data\\Parameter\\growth_scan_300_900.vspm"
start_nm = 300.0
stop_nm = 900.0
step_nm = 1.0

# 只有与 LabSolutions 方法界面核对后才填写。
# scan_speed_nm_per_min = 600.0
```

登记扫描速度后，程序会报告标称时间：

```text
相邻点标称时间 = step_nm * 60 / scan_speed_nm_per_min
标称扫描时间   = abs(stop_nm - start_nm) * 60 / scan_speed_nm_per_min
```

这些估算不包含仪器响应、附件动作、命令处理、保存和自动导出耗时。

### 重复完整光谱

`--interval-seconds` 表示相邻两次 `Command=111` 的目标开始时间差，不是“上一次完成后再等待多少秒”。程序采用单调时钟，记录计划偏移、实际开始偏移和迟到量。

如果前一次测量、保存或导出超过计划时间和允许容差，序列会停止，不发送下一条测量命令。

### 固定波长时间曲线

如果实验需要固定一个或多个波长的高时间分辨率曲线，应在 LabSolutions Time Course 中建立 `.vtcm` 方法。自动控制手册的 `Command=400/410/411` 负责加载方法、设置样品和开始测量；采样波长、时间间隔和总时长仍由 `.vtcm` 定义。

更完整的说明见 [扫描请求与 profile 解析](docs/profile-resolution.md)、[时间步长控制](docs/time-step-control.md) 和 [波长控制](docs/wavelength-control.md)。

## 控制电脑要求

- Windows PowerShell 5.1 或更高版本
- Python 3.11 或更高版本
- 已安装并授权自动控制功能的 LabSolutions UV-Vis
- 已由操作人员手动验证的 `.vspm` Spectrum 方法
- 已配置的命令目录、数据目录、自动导出目录和日志目录

核心运行时没有第三方 Python 依赖，控制电脑不需要联网安装 Python 包。

## 快速开始

### 1. 获取并初始化

```powershell
git clone https://github.com/chemyibinjiang/shimadzu-uvvis-automation.git
cd shimadzu-uvvis-automation

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\setup-control-pc.ps1 `
  -MethodFile C:\UVVis-Data\Parameter\growth_scan_300_900.vspm `
  -ScanStartNm 300 `
  -ScanStopNm 900 `
  -ScanStepNm 1
```

初始化脚本会：

- 创建本地 `.venv`，但不下载第三方依赖
- 创建命令、数据、导出和审计目录
- 生成被 Git 忽略的 `control-pc.toml`
- 保留已有配置，除非显式加入 `-Force`

如果已经在 LabSolutions 中核对扫描速度，可在首次初始化时额外传入 `-ScanSpeedNmPerMinute <数值>`。不要根据仪器型号猜测该值。

### 2. 运行软件模拟验收

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test-simulator.ps1
```

通过标准：

```text
SIMULATOR ACCEPTANCE PASSED
```

报告位于 `runtime\simulator-acceptance\<时间>\reports`。模拟方法、原始数据和导出文件均带有 `SIMULATED` 标记，不能作为实验数据。

### 3. 配置 LabSolutions

1. 在 LabSolutions UV-Vis 中保存并人工验证 Spectrum 参数文件 `.vspm`。
2. 在 `Tools -> Customize -> Automatic Control` 设置命令接收目录。手册默认目录为 `C:\UVVisControl`。
3. 设置测量后自动导出 CSV、TXT 或 Excel 到 `C:\UVVis-Data\Export`。
4. 让导出文件名包含或以 `SampleID` 开头。
5. 打开 `Instrument -> Automatic Control`，保持窗口处于等待命令状态。

推荐目录：

```text
C:\UVVisControl
C:\UVVis-Data\Parameter
C:\UVVis-Data\Data
C:\UVVis-Data\Export
C:\UVVis-Automation\Logs
```

首次验收建议只使用 ASCII 路径、样品名和 SampleID。

### 4. 分级验证真实通道

先检查文件系统、配置和权限，不发送 LabSolutions 命令：

```powershell
.\scripts\test-live.ps1
```

LabSolutions 已进入自动控制等待状态后，只发送 `Command=0`：

```powershell
.\scripts\test-live.ps1 -Ping
```

`Ping` 不会连接仪器、移动附件、校正或测量。

### 5. 首次单光谱

先查看完整命令计划：

```powershell
.\scripts\run-test-measurement.ps1 `
  -SampleName validation_blank `
  -Profile default
```

确认当前样品池、样品、方法、数据路径和自动导出设置后才执行：

```powershell
.\scripts\run-test-measurement.ps1 `
  -SampleName validation_blank `
  -Profile default `
  -Execute
```

仪器尚未连接时加入 `-Connect`。只有空白已经正确放置并且确认需要校正时才加入 `-AutoCorrection`。

也可以使用旧控制器风格的范围入口：

```powershell
.\scripts\uvvis.ps1 spectrum `
  --start 300 `
  --stop 900 `
  --step 1 `
  --wavelengths 450 520 650 `
  --sample-name validation_sample
```

该命令只会匹配已登记的方法。`--wavelengths` 用于验证目标点位于完整 Spectrum 的范围和数据网格中。

### 6. 生长过程重复光谱

先查看序列计划：

```powershell
.\scripts\run-growth-series.ps1 `
  -SampleName Au_growth `
  -SeriesId Au_growth_20260711_001 `
  -Profile default `
  -Count 10 `
  -IntervalSeconds 90
```

确认每次 SampleID、数据文件、计划时间和安全参数后执行：

```powershell
.\scripts\run-growth-series.ps1 `
  -SampleName Au_growth `
  -SeriesId Au_growth_20260711_001 `
  -Profile default `
  -Count 10 `
  -IntervalSeconds 90 `
  -Execute
```

序列生成 `Au_growth_20260711_001_0001` 到 `..._0010`，每次采集均有独立原始数据、导出匹配和 run manifest。

时间间隔必须明显大于现场测得的完整扫描、保存和导出周期。首次真实序列只运行三次，并保持操作人员在场。

## 安全默认值

首次测量和生长序列包装脚本默认采用：

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| `MeasurementMode` | `2` | 多联池时只测当前样品池 |
| `Discharge` | `OFF` | 不请求抽吸附件在测量后排出 |
| `correction` | `none` | 不自动执行基线校正或调零 |
| `disconnect` | `false` | 测量后不主动断开仪器 |
| 执行模式 | plan only | 必须显式加入执行开关 |

岛津手册说明，`MeasurementMode=1` 会测量全部已配置样品池；使用抽吸附件时，如果省略 `Discharge`，软件可能按启用处理。因此本项目始终显式发送这些值。

通用 `send` 命令可能移动附件或改变仪器状态，应以当前版本自动控制手册、实验室 SOP 和现场工程师确认结果为准。

## 数据、导出与审计

默认日志目录：

```text
C:\UVVis-Automation\Logs
```

| 输出 | 内容 |
| --- | --- |
| `<日期>\..._cmd<N>_<request-id>.json` | 单条命令、参数、反馈、耗时和异常 |
| `runs\<SampleID>.json` | 单次测量、数据路径、时间和导出 SHA-256 |
| `series\<SeriesID>.json` | 序列计划、实际开始偏移、迟到量和各次结果 |
| `C:\UVVis-Data\Data\<SampleID>.vspd` | LabSolutions 原始 Spectrum 数据 |
| CSV/TXT/XLSX | LabSolutions 按预设格式自动导出的结果 |

序列模式要求每次导出匹配模式唯一。推荐配置：

```toml
[export]
directory = "C:\\UVVis-Data\\Export"
pattern = "{sample_id}*.csv"
timeout_seconds = 120.0
stable_seconds = 2.0
```

## 超时与恢复

超时不代表命令一定没有执行。不要立即重发 `Command=111`。

先查看恢复状态：

```powershell
.\scripts\uvvis.ps1 recover
```

确认 LabSolutions 已结束当前动作，并核对命令文件、反馈文件、仪器界面和已生成数据后，先查看清除计划，再执行清除：

```powershell
.\scripts\uvvis.ps1 recover --clear
.\scripts\uvvis.ps1 recover --clear --execute
```

只有没有匹配反馈、但操作人员已经从仪器和 LabSolutions 确认最终状态时，才考虑 `--force`。恢复动作也会写入审计目录。

## 常用命令

```powershell
# 文件系统和配置诊断
.\scripts\uvvis.ps1 doctor --write-check

# Hello 通道测试
.\scripts\uvvis.ps1 ping

# 单次 Spectrum 计划
.\scripts\uvvis.ps1 spectrum --profile default --sample-name sample_01

# 重复 Spectrum 计划
.\scripts\uvvis.ps1 series `
  --profile default `
  --sample-name growth `
  --series-id growth_01 `
  --count 10 `
  --interval-seconds 90

# 通用命令计划，不会立即发送
.\scripts\uvvis.ps1 send 12 CellPosition=3

# 确认后才发送通用命令
.\scripts\uvvis.ps1 send 12 CellPosition=3 --execute
```

## 文档

- [AI tutor 的 UV-Vis MCP 服务](docs/mcp-server.md)
- [LabSolutions 自动控制交接说明](docs/labsolutions-operation.md)
- [设备连接来源记录](docs/vendor-communication-and-manual-notes.md)
- [控制电脑现场验收手册](docs/control-pc-acceptance.md)
- [扫描请求与 LabSolutions profile 解析](docs/profile-resolution.md)
- [波长范围与多波长控制](docs/wavelength-control.md)
- [时间步长、重复光谱与 Time Course](docs/time-step-control.md)
- [版本记录](CHANGELOG.md)

## 开发与打包

运行单元测试：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

运行完整模拟验收：

```powershell
.\scripts\test-simulator.ps1
```

从已提交且工作区干净的版本生成控制电脑 ZIP：

```powershell
.\scripts\build-control-pc-bundle.ps1
```

打包脚本使用 `git archive`，不会包含 `.git`、`.venv`、`runtime`、本机 `control-pc.toml` 或实验数据。

## 项目边界

本项目是 LabSolutions 上层文本交换客户端，不是岛津底层驱动，也不替代仪器联锁、实验室 SOP、校准流程或现场工程师确认。

以下能力必须单独完成真实控制电脑和仪器验收后才能用于实验：

- LabSolutions DB/CS 文件标识和权限
- Photometric 离散多波长方法
- Time Course `.vtcm/.vtcd` 流程
- 多联池批量测量
- 自动进样、抽吸、排出和清洗附件
- 与反应器或生长系统联动
- 长时间无人值守运行

许可证见 [LICENSE](LICENSE)。
