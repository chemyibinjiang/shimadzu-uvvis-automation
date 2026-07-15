# LabSolutions UV-Vis 时间步长控制

## 1. 先区分三种“时间步长”

| 需求 | 控制位置 | 本项目如何处理 |
| --- | --- | --- |
| 连续光谱中相邻波长点的时间间隔 | LabSolutions `.vspm` 中的数据间隔、扫描速度和响应设置 | 读取人工登记的元数据并计算标称值，不修改 `.vspm` |
| 生长过程中每隔一段时间重新采集完整光谱 | 上层调度器 | `series --interval-seconds`，按 `Command=111` 开始时刻进行 start-to-start 调度 |
| 固定一个或多个波长的连续时间曲线 | LabSolutions Time Course `.vtmm`（手册示例为 `.vtcm`） | MCP 已规划波长、间隔和总时长；方法生成及现场执行尚未开放 |

旧 `xiaozhi_uv_edu` 控制器的 `settle_time` 是在每次直接移动单色器后执行 `sleep`，然后读取能量值。LabSolutions Spectrum 的文本命令 `Command=111` 只有 `MeasurementMode` 和 `Discharge`，没有等价的每波长 `settle_time` 参数。

## 2. 连续光谱内部的标称时间

先在 LabSolutions 中打开实际 `.vspm` 方法，人工核对并记录：

- 起始波长
- 终止波长
- 数据间隔
- 扫描速度
- 响应时间或响应模式
- 狭缝、附件和扫描方向

把已经核对的扫描速度登记到 `control-pc.toml`：

```toml
[scan_profiles.default]
method_file = "D:\\UVVis-Automation\\methods\\growth_scan_300_900.vspm"
start_nm = 300.0
stop_nm = 900.0
step_nm = 1.0
scan_speed_nm_per_min = 600.0
```

标称相邻数据点时间为：

```text
nominal_point_interval_seconds = step_nm * 60 / scan_speed_nm_per_min
```

上例为 `1 * 60 / 600 = 0.1 s`。标称波长扫过时间为：

```text
abs(stop_nm - start_nm) * 60 / scan_speed_nm_per_min = 60 s
```

这些数值不包含仪器响应、命令处理、附件动作、数据保存和自动导出耗时，因此不能当作经过校准的实际采样时间戳。执行计划会把它们放在 `wavelength_control.within_scan_timing` 中供检查。

没有在 LabSolutions 界面核对扫描速度时，不要猜测数值。省略 `scan_speed_nm_per_min` 后，程序会明确显示标称时间未知。

## 3. 重复完整光谱

先只查看计划：

```powershell
.\scripts\run-growth-series.ps1 `
  -SampleName Au_growth `
  -SeriesId Au_growth_20260711_001 `
  -Profile default `
  -Count 10 `
  -IntervalSeconds 90
```

该计划会生成：

```text
Au_growth_20260711_001_0001
Au_growth_20260711_001_0002
...
Au_growth_20260711_001_0010
```

每个时间点有独立的 `.vspd`、自动导出文件和 run manifest。确认样品、方法、当前样品池、计划时间和导出命名后才执行：

```powershell
.\scripts\run-growth-series.ps1 `
  -SampleName Au_growth `
  -SeriesId Au_growth_20260711_001 `
  -Profile default `
  -Count 10 `
  -IntervalSeconds 90 `
  -Execute
```

也可以直接调用 CLI：

```powershell
.\scripts\uvvis.ps1 series `
  --sample-name Au_growth `
  --series-id Au_growth_20260711_001 `
  --profile default `
  --count 10 `
  --interval-seconds 90
```

## 4. `interval-seconds` 的准确含义

`interval-seconds` 是相邻两次 `Command=111` 的目标开始时间差，不是“上一次完成后再等待多少秒”。程序使用单调时钟，避免系统时钟调整造成调度跳变。

每次执行顺序为：

```text
设置本次 SampleID 和数据文件
等待本次计划开始时刻
发送 Command=111
等待测量反馈
等待与 SampleID 对应的导出文件稳定
准备下一次测量
```

如果前一次扫描、保存或导出过慢，下一次已经超过计划时刻和容差，程序会在发送下一条 `Command=111` 之前停止。默认容差是 `1 s`，可用 `-OverrunToleranceSeconds` 或 `--overrun-tolerance-seconds` 修改。停止后不要立即重发；先检查已完成的原始数据、导出文件、审计记录和 LabSolutions 状态。

## 5. 如何选择时间间隔

1. 先人工监护执行一次单光谱，读取 run manifest 的 `started_at_utc`、`completed_at_utc` 和 `elapsed_seconds`。
2. 同时记录 LabSolutions 中实际扫描、保存和导出的完成时点。
3. 选择明显大于最慢完整周期的间隔，并保留仪器和文件系统波动余量。
4. 先执行三次短序列，确认 `start_lateness_seconds`、文件关联和导出稳定性。
5. 再增加总时长；不要把容差调大来掩盖持续超时。

如果配置了扫描速度，计划会在 `interval-seconds` 不长于标称波长扫过时间时给出警告。即使没有警告，实际扫描和导出仍可能更慢，应以现场测量为准。

## 6. 固定波长随时间测量

如果目标是固定 `450 nm`，或固定多个波长观察随时间变化，LabSolutions Time Course 通常比反复扫描整个 `300-900 nm` 更合适。自动控制手册第 5.14 和 6.4 节给出的流程是：

```text
Command=400  加载 .vtmm 参数文件（手册版本可能为 .vtcm）
Command=410  设置样品和数据文件信息
Command=411  执行时间程序测定
```

`Command=411` 同样只公开 `MeasurementMode` 和 `Discharge`。采样波长、时间间隔和总时长需要预先保存在 Time Course 方法中，不能通过这条文本命令临时传入。

当前仓库的 `plan_uvvis_measurement` 已能校验 Time Course 请求、选择模板并给出 `400/410/411` 命令计划，但尚未开放执行，因为仍需验收本机 `.vtmm/.vtmd`、多波长结果结构和自动 CSV/Excel 导出完成时点。在完成这些现场检查前，使用 `series` 采集重复完整光谱，或由操作人员直接运行已验证的 Time Course 方法。

## 7. 输出与审计

成功序列会写入：

```text
D:\UVVis-Automation\logs\runs\<SampleID>.json
D:\UVVis-Automation\logs\series\<SeriesID>.json
```

序列结果记录计划偏移、实际开始偏移、开始迟到量、单次耗时、命令反馈、数据路径、导出文件大小和 SHA-256。LabSolutions 原始数据和软件界面仍是判断仪器是否实际完成动作的依据。
