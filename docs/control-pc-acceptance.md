# 岛津 UV-Vis 控制电脑现场验收手册

本文用于把 `shimadzu-uvvis-automation` 部署到安装了 LabSolutions UV-Vis 的控制电脑，并按风险从低到高完成验收。

## 1. 验收边界

验收分为四级：

| 级别 | 动作 | 是否影响仪器 |
| --- | --- | --- |
| A | 本地模拟器端到端测试 | 否 |
| B | 目录、配置和写权限诊断 | 否 |
| C | LabSolutions `Command=0` Hello | 否，不连接和测量 |
| D | 当前样品池的首次 Spectrum 测量 | 是 |

必须依次通过 A、B、C 后才能进入 D。

## 2. 验收前准备

- Windows PowerShell 5.1 或更高版本
- Python 3.11 或更高版本
- 已安装并授权自动控制功能的 LabSolutions UV-Vis
- 已由操作人员手动验证的 `.vspm` Spectrum 方法
- 一个空白样品和一个已知响应的验证样品
- 明确的命令目录、数据目录、自动导出目录和日志目录
- 首次测试期间有熟悉仪器的操作人员在场

推荐目录：

```text
C:\UVVisControl
C:\UVVis-Data\Parameter
C:\UVVis-Data\Data
C:\UVVis-Data\Export
C:\UVVis-Automation\Logs
```

路径建议先只使用 ASCII 字符。

## 3. A 级：软件模拟验收

在仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\setup-control-pc.ps1 `
  -MethodFile C:\UVVis-Data\Parameter\growth_scan_300_900.vspm

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test-simulator.ps1
```

通过标准：

- 显示 `SIMULATOR ACCEPTANCE PASSED`
- `doctor.json` 的顶层 `ok` 为 `true`
- `ping.json` 的 `return_code` 为 `0`
- `plan.json` 显示 `MeasurementMode=2` 和 `Discharge=OFF`
- `measurement.json` 包含模拟 CSV 的大小和 SHA-256
- `audit\runs` 中存在对应的 run manifest

失败时先查看同一运行目录下的 `simulator.stderr.log`。

## 4. 配置 LabSolutions

1. 启动 Spectrum 测定程序。
2. 打开 `Tools -> Customize -> Automatic Control`。
3. 把命令接收目录设置为 `control-pc.toml` 中的 `command_dir`。
4. 在 LabSolutions 中设置测量后自动导出 CSV、TXT 或 Excel。
5. 把导出目录设置为 `control-pc.toml` 中的 `[export].directory`。
6. 让导出文件名尽可能包含或以 `SampleID` 开头。
7. 打开 `Instrument -> Automatic Control`。
8. 确认自动控制窗口显示等待状态并保持该窗口打开。

手册默认命令目录为 `C:\UVVisControl`。Spectrum 使用：

```text
SPC_CMD.txt
SPC_RES.txt
```

LabSolutions 正在执行一条命令时不能接收下一条命令。本项目会等待匹配反馈并使用进程间锁，仍应确保没有其他上位机程序同时写入该目录。

## 5. B 级：控制电脑诊断

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test-live.ps1
```

该脚本不会创建 `SPC_CMD.txt`，只会用随机 `.tmp` 文件检查目录读写权限。

通过标准：

- 顶层 `ok` 为 `true`
- `command_directory_write`、`export_directory_write`、`data_directory_write` 和 `audit_directory_write` 均为 `pass`
- `controller_lock` 为 `pass`，表示没有另一个控制进程占用命令目录
- `recovery_state` 为 `pass`，表示没有状态不明的上一条命令
- `method_file` 为 `pass`
- `pending_command` 为 `pass`
- `measurement_mode` 显示当前样品池模式
- `discharge` 显示 OFF

`automatic_control` 在此阶段显示 `warn` 是正常的，因为只有下一阶段的 Hello 能确认 LabSolutions 正在监听。

## 6. C 级：Hello 通道测试

确认 LabSolutions 已显示自动控制等待状态，然后运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test-live.ps1 -Ping
```

该步骤只发送：

```text
Command=0
```

通过标准：

- 显示 `LIVE HELLO TEST PASSED`
- `return_code` 为 `0`
- 日志目录产生一条 `cmd0` 审计记录

超时判断：

- `SPC_CMD.txt` 仍存在：LabSolutions 没有读取命令。检查自动控制窗口和命令目录是否一致。
- `SPC_CMD.txt` 已消失但没有 `SPC_RES.txt`：LabSolutions 已读取但未正常写回，检查软件状态和权限。
- 存在旧的 `SPC_RES.txt`：先保存现场证据；下一次命令会归档其文本后再清理。

## 7. D 级：首次 Spectrum 测量

### 7.1 人工确认

- 当前样品池位置正确
- 验证样品或空白已放置
- 方法文件中的波长范围、扫描速度、附件和保存设置已人工检查
- 自动导出命名规则与 `[export].pattern` 一致
- 没有其他程序控制命令目录

### 7.2 只查看命令计划

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run-test-measurement.ps1 `
  -SampleName validation_blank
```

该命令不执行测量。计划应包含：

```text
Command=0
Command=100
Command=110
Command=111 MeasurementMode=2 Discharge=OFF
```

### 7.3 执行测量

仪器已连接且方法不需要重新校正时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run-test-measurement.ps1 `
  -SampleName validation_blank `
  -Execute
```

仪器未连接时加入 `-Connect`。只有空白已经正确放置且确认需要基线校正时，才加入 `-AutoCorrection`。

首次脚本强制：

- `MeasurementMode=2`
- `Discharge=OFF`
- 不断开仪器
- 未指定时不校正

通过标准：

- 每条命令 `return_code=0`
- `C:\UVVis-Data\Data` 中产生新的 `.vspd`
- 自动导出目录产生与本次 SampleID 对应的新文件
- 输出包含导出文件大小和 SHA-256
- `Logs\runs\<SampleID>.json` 存在且顶层 `ok=true`
- 人工核对 LabSolutions 界面、原始数据和导出数据一致

## 8. 常见错误与处置

| Return | 含义 | 首要检查 |
| ---: | --- | --- |
| `-1000` | 参数文件加载失败 | `.vspm` 路径、权限和方法兼容性 |
| `-1001` | 未加载参数文件 | `Command=100` 是否成功 |
| `-3106` | 多联池未初始化 | 附件配置；首次测试保持当前池模式 |
| `-3200` | 测量过程错误 | 仪器状态、样品室、附件和 LabSolutions 提示 |
| `-3206` | 操作员中止 | 记录为人工中止，不自动重试 |
| `-1201` | 数据文件名无效 | 数据目录、扩展名、ASCII 路径和权限 |
| `-1202` | 同名数据文件已加载 | 使用新的 SampleID，不覆盖旧数据 |
| `-1203` | 光谱数据保存失败 | 磁盘、目录权限、文件占用和剩余空间 |

发生任何错误后：

1. 不要立即重发 `Command=111`。
2. 保存控制台输出、审计 JSON、`SPC_CMD.txt`/`SPC_RES.txt` 状态和 LabSolutions 截图。
3. 确认仪器是否已经实际完成测量。
4. 解决原因后使用新的 SampleID 重新测试。

如果输出提示需要恢复，先只读查看：

```powershell
.\scripts\uvvis.ps1 recover
```

当命令文件已消失且反馈与恢复标记中的命令编号一致时，可以执行：

```powershell
.\scripts\uvvis.ps1 recover --clear
.\scripts\uvvis.ps1 recover --clear --execute
```

第一条只显示计划，第二条才清除标记。没有匹配反馈或命令文件仍存在时会拒绝普通清除；`--force` 只用于操作人员已经从仪器界面确认最终状态的例外情况。恢复动作会单独写入审计目录。

## 9. 进入自动化前的退出条件

以下条件全部满足后，才考虑连接自动进样、反应器或生长实验编排：

- 连续完成至少三次人工监护测量
- 每次原始数据、导出文件、审计记录和 SampleID 一一对应
- 断线、超时、保存失败和人工中止均有明确恢复步骤
- 附件位置、抽吸和排出行为已单独验收
- 实验室负责人确认允许无人值守及其联锁要求
