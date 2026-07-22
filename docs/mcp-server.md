# UV-Vis MCP 服务

`shimadzu-uvvis-mcp` 是供 AI tutor 调用的 stdio MCP 服务。当前提供十个工具。

只读规划与查询：

- `plan_uvvis_measurement`：先选择兼容模式，再统一规划 Spectrum、Photometric、Quantitation 和 Time Course。
- `plan_uvvis_sample_batch`：规划需要人工逐个换样的多样品批次和独立数据目录。
- `plan_uvvis_scan`：兼容旧调用，精确匹配已登记的 Spectrum profile。
- `get_uvvis_batch_status`：读取持久化的 Spectrum 批次状态和下一个样品。

Spectrum 批次执行：

- `generate_uvvis_method`：从只读模板生成并读回验证 Spectrum `.vspm`，不校正基线或测量。
- `start_uvvis_batch`：创建批次、Hello、按配置连接并加载 `.vspm`。
- `correct_uvvis_baseline`：确认空白放置后发送 `Command=21`。
- `measure_next_uvvis_sample`：确认换样后测量清单中的下一个样品。
- `recover_uvvis_spectrum_result`：从已测得的 `.vspd` 恢复并发布 Spectrum 结果，不重测。
- `abort_uvvis_batch`：在等待状态终止后续批次动作，不发送物理中止命令。

三个规划工具和状态查询不会写命令。方法生成器会改变方法文件和 LabSolutions 界面状态，但不会
发送基线或测量命令。三个物理批次动作和终止工具必须遵守各自的显式确认字段。

## 安装与启动

控制电脑已经安装 MCP SDK 时，可离线安装本仓库：

```powershell
python -c "import mcp; print(mcp.__file__)"
python -m pip install -e . --no-build-isolation --no-deps
shimadzu-uvvis-mcp --config D:\UVVis-Automation\control-pc.toml
```

`--no-build-isolation` 避免 pip 创建隔离环境并联网下载 `setuptools`；`--no-deps` 避免访问
软件源解析依赖。MCP SDK 本身仍必须已经安装。

也可以固定配置文件：

```powershell
$env:SHIMADZU_UVVIS_CONFIG = "D:\UVVis-Automation\control-pc.toml"
shimadzu-uvvis-mcp
```

MCP 客户端配置示例：

Codex 或使用同类 TOML 配置的 AI tutor：

```toml
[mcp_servers.shimadzu-uvvis]
type = "stdio"
command = "C:/Users/11979/anaconda3/python.exe"
args = ["-m", "shimadzu_uvvis.mcp_server", "--config", "D:/UVVis-Automation/control-pc.toml"]
startup_timeout_sec = 30
tool_timeout_sec = 900

[mcp_servers.shimadzu-uvvis.env]
PYTHONIOENCODING = "utf-8"
```

可直接使用仓库中的 [`mcp-client.example.toml`](../mcp-client.example.toml)。该客户端配置不能合并
到 `D:\UVVis-Automation\control-pc.toml`：前者负责启动 MCP 进程，后者由 MCP 进程读取并配置
LabSolutions 路径和方法。

使用 JSON MCP 配置的客户端：

```json
{
  "mcpServers": {
    "shimadzu-uvvis": {
      "command": "shimadzu-uvvis-mcp",
      "args": ["--config", "D:\\UVVis-Automation\\control-pc.toml"]
    }
  }
}
```

## plan_uvvis_measurement

统一输入字段：

| 字段 | 适用模式 | 含义 |
| --- | --- | --- |
| `mode` | 全部 | 默认 `auto`；也可显式指定 `spectrum/photometric/quantitation/time_course` |
| `measurement_purpose` | 自动路由 | 默认 `measurement`；标准曲线或浓度测定使用 `quantitation` |
| `signal_type` | 全部 | 默认 `absorbance`，必须匹配登记模板 |
| `template_name` | 全部 | 有多个同类模板时显式指定 |
| `start_nm/stop_nm/step_nm` | Spectrum | 连续扫描范围和数据间隔 |
| `direction` | Spectrum | 可选 `ascending/descending` |
| `wavelengths_nm` | Photometric | 离散波长数组 |
| `wavelength_nm` | Quantitation、Time Course | 测定波长 |
| `interval_seconds/duration_seconds` | Time Course | 采样间隔和总时长 |

示例：

AI tutor 收到 `400-700 nm，步长 10 nm` 时不必预先指定模式：

```json
{
  "start_nm": 400,
  "stop_nm": 700,
  "step_nm": 10
}
```

规划器会在启动 LabSolutions 前检查 Spectrum 1.13 的能力，返回
`routing.selected_mode=photometric` 和 `400, 410, ..., 700` 共 31 个精确波长点。结果同时返回
四个 `method_generation.segments`，点数为 `10/10/10/1`；每个目标 `.vphm` 都存在并通过证明记录
后，`routing.current_mcp_execution_supported=true`。

动力学示例：

```json
{
  "mode": "time_course",
  "wavelength_nm": 520,
  "interval_seconds": 1,
  "duration_seconds": 600
}
```

结果包括规范化请求、模板、目标方法文件、数据扩展名、路径就绪检查和模式对应的命令计划。
`routing` 字段记录调用方请求的模式、最终选择的模式和选择依据。模式选择永远发生在方法生成、
Automatic Control、基线校正和测量之前。
`execution_readiness` 还包含 `mcp_execution_supported`；当前通过能力校验的 Spectrum 和
Photometric 请求为 `true`。Quantitation 和 Time Course 仍不会仅因磁盘上存在方法而被标记为可执行。
目标方法不存在时返回：

```json
{
  "status": "method_generation_required",
  "plan_only": true,
  "method_generation": {
    "required": true,
    "automatic_generation_supported": true
  }
}
```

这不是错误地把模板直接执行，而是明确告诉 AI tutor：必须先在 LabSolutions 中生成并验证该参数
组合的方法。完整说明见 [四种测量模式与方法模板](four-mode-methods.md)。

## generate_uvvis_method

当前自动生成支持 Spectrum 和 Photometric 的 `signal_type=absorbance`。工具使用 Windows 菜单和稳定控件
ID，不使用 Computer Use、截图坐标，也不修改 OLE 二进制字节。流程为：

1. 校验模板 SHA-256，确认没有活动 Spectrum 批次；
2. 验证 Automatic Control Waiting 和 `Command=0/Return=0`；
3. 离开 Automatic Control，通过岛津驱动连接 UV-2700i；
4. 加载只读模板，在参数编辑器中设置高波长、低波长和数据间隔；
5. Save As 到 `D:\UVVis-Automation\methods\generated`；
6. 重新打开目标 `.vspm` 并读回参数；
7. 写入同名 `.vspm.generation.json` 完整性记录；
8. 断开编辑会话并恢复 Automatic Control Waiting 和 Hello。

本机 Spectrum 1.13 的“数据间隔”只提供：

```text
0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0 nm
```

因此 `400-700 nm, step 10 nm` 不能生成真实的 10 nm `.vspm`。程序会明确拒绝，不会静默改成
5 nm。需要精确测量 `400, 410, ..., 700 nm` 时，程序使用 Photometric 离散波长方法；本机单方法
10 点上限使该请求自动生成四个经过读回验证的 `.vphm`。LabSolutions Spectrum 通常从高波长扫向低波长，因此上层的 `400 -> 700` 会写成
编辑器中的 `开始波长=700`、`结束波长=400`，读回记录会同时保留这一映射。

## plan_uvvis_sample_batch

当仪器只有一个样品位和一个参比位时，不同样品必须顺序测量。此工具复用
`plan_uvvis_measurement` 的四模式参数校验，并给每个样品分配唯一运行 ID、目录和人工确认门禁。

```json
{
  "batch_id": "experiment_20260716_001",
  "mode": "spectrum",
  "samples": [
    {"sample_name": "sample A", "sample_id": "sample_a"},
    {"sample_name": "sample B", "sample_id": "sample_b"}
  ],
  "reference_name": "blank",
  "baseline_policy": "new",
  "start_nm": 400,
  "stop_nm": 700,
  "step_nm": 1
}
```

规划结果把输入样品依次转换为 `001_sample_a`、`002_sample_b`。每个样品都包含状态为
`required` 的 `replace_sample_and_confirm` 门禁；后续执行器只有收到该样品的现场确认后，才可发送
对应的 `111/211/311/411` 测量命令。规划调用本身永远不能充当确认。

`baseline_policy=new` 会在样品循环之前规划 `place_blank_and_confirm` 门禁和
`Command=21, CorrectionType=1`。校正成功后，同一批次的后续样品直接复用该基线，不再要求基线
确认。`baseline_policy=reuse_valid` 用于直接沿用当前仪器会话中同一方法、空白和参比对应的有效
基线；该策略不设置操作员门禁，也不再次发送校正命令。方法或参比发生变化时，上层必须建立新
批次并使用 `baseline_policy=new`。

目标目录已存在时返回 `path_conflict`，防止覆盖旧实验。详细的目录结构和现场流程见
[多样品顺序测量](sample-batches.md)。

## plan_uvvis_scan

兼容工具输入：

```json
{
  "start_nm": 400,
  "stop_nm": 700,
  "step_nm": 1,
  "direction": null,
  "profile_name": null
}
```

它只匹配 `[scan_profiles.<name>]` 中参数完全一致的已验证 `.vspm`。没有匹配或存在歧义时
拒绝规划，不会自动截取更大范围的方法，也不会生成新方法。

## Spectrum 批次执行工具

### start_uvvis_batch

输入与 Spectrum 批次规划相同，并额外要求：

```json
{
  "batch_id": "experiment_20260716_001",
  "samples": [
    {"sample_name": "sample A", "sample_id": "sample_a"},
    {"sample_name": "sample B", "sample_id": "sample_b"}
  ],
  "reference_name": "blank",
  "start_nm": 400,
  "stop_nm": 700,
  "step_nm": 1,
  "baseline_policy": "new",
  "execution_confirmed": true
}
```

工具会重新构建并校验计划。只有参数对应的 generated `.vspm` 已存在、模板及目录就绪且目标批次
目录不存在时才会继续。然后 `LabSolutionsRuntimeManager` 启动或定位 Spectrum，核对并在需要时设置
`D:\UVVis-Automation\control`，进入 Automatic Control Waiting，并要求 `Command=0` 返回
`Return=0`。任一步失败都不会创建批次或加载方法。成功后状态为 `WAITING_FOR_BLANK`；不会在此调用
中校正或测样品。

`Command=100` 或 `Command=300` 加载参数后若出现“参数文件已改变，是否执行基线校正”提示，后台
会用 Win32 控件消息自动选择“否”，并在批次事件中记录
`parameter_change_baseline_prompt_declined`。提示无法关闭时批次进入 `RECOVERY_REQUIRED`，不会等待
人工点击，也不会误把该提示中的“是”当成正式基线校正。

该执行入口也会先使用自动模式路由。若范围参数被选为 Photometric，例如 `400-700/10 nm`，
执行器会启动 Photometric，并在一次样品确认后依次执行所有方法段；不会降级为 5 nm Spectrum。

### correct_uvvis_baseline

```json
{
  "batch_id": "experiment_20260716_001",
  "blank_loaded_confirmed": true
}
```

只接受 `WAITING_FOR_BLANK`。发送物理命令前再次要求 Waiting 和 `Command=0/Return=0`，但此阶段
禁止修改命令目录或重启软件；运行时不一致时保持等待状态并报错。随后发送
`Command=21, CorrectionType=1`。成功后进入
`WAITING_FOR_SAMPLE`。`baseline_policy=reuse_valid` 只有在程序保存的基线记录与方法哈希、参比和
当前仪器会话一致时才会跳过此步骤。即使配置了 `connect_before_run=true`，程序也会检查
`Command=1` 的结果：`Return=-3002` 表示仪器原本已经连接，可以复用；`Return=0` 表示本次刚建立
了新连接，旧基线立即失效，批次自动转为 `WAITING_FOR_BLANK`，不会开始样品测量。

### measure_next_uvvis_sample

```json
{
  "batch_id": "experiment_20260716_001",
  "sample_id": "001_sample_a",
  "sample_loaded_confirmed": true
}
```

`sample_id` 必须与状态查询返回的 `next_sample.sample_id` 完全一致。每个样品也先执行只验证的
Waiting/Hello 门禁。Spectrum 发送 `110/111`；Photometric 对每个最多 10 点的方法段发送
`300/310/311/320/321`。程序在 `320` 保存、`321` 关闭后直接解析 `.vphd` 的命名吸光度列，合并 Photometric 点表、生成 CSV、JSON、PNG 和
最大吸收波长结果，复制到样品目录及仓库 `outputs`，然后才允许下一个样品。最后
一个样品完成后进入 `COMPLETED`。

Spectrum 在 `111` 后优先直接解析 `.vspd` 的 X/Y 数据流，无法识别该结构时才等待 LabSolutions
自动导出的 CSV。校验完整波长网格后生成标准 CSV、JSON、PNG 和最大吸收波长结果。只有原始
`.vspd`、标准结果及 `outputs` 发布全部成功后才把样品标记为 `COMPLETED`；解析或发布失败时进入
`RECOVERY_REQUIRED`，已记录 `111/Return=0` 且原始文件完整时可只恢复结果，禁止重新测量。

### recover_uvvis_spectrum_result

```json
{
  "batch_id": "experiment_20260716_001"
}
```

只接受因 Spectrum 测量已成功但等待自动 CSV 导出超时而进入 `RECOVERY_REQUIRED` 的批次。工具
校验已记录的 `Command=111/Return=0` 和现有 `.vspd`，直接生成 CSV、JSON、PNG 并发布到仓库
`outputs`，随后完成当前样品。该工具不接受换样或执行确认字段，因为它不会发送任何
LabSolutions 或仪器命令，也不会重新测量。

### get_uvvis_batch_status

返回状态、基线记录、已完成数量、下一个样品、最近错误和 `next_action`。它只读取原子写入的
`batch-manifest.json`。

### abort_uvvis_batch

```json
{
  "batch_id": "experiment_20260716_001",
  "reason": "student ended the experiment",
  "abort_confirmed": true
}
```

只允许从 `WAITING_FOR_BLANK` 或 `WAITING_FOR_SAMPLE` 转为 `ABORTED`。它不会中断正在执行的
LabSolutions 命令，也不能清除 `RECOVERY_REQUIRED`；状态不确定时必须先检查 LabSolutions 和现有
恢复记录。

### LabSolutionsRuntimeManager

控制电脑配置必须显式启用：

```toml
[runtime]
enabled = true
executable = "D:\\UVNavi.exe"
arguments = ["/APP:Spectrum"]
startup_timeout_seconds = 30.0
ui_timeout_seconds = 15.0
ui_message_timeout_seconds = 5.0
hello_timeout_seconds = 15.0
configure_command_directory = true
```

本控制电脑的方法生成器结束时会断开编辑会话，因此生产配置使用
`[spectrum].connect_before_run=true`。新批次在任何基线或测量前先发送 `Command=1`；USB 已插入
并不等于 LabSolutions 已建立仪器连接。`Command=0` 只验证自动控制文件通道。

运行时管理器使用 Windows 窗口、菜单和控件 API，不使用截图坐标或 Computer Use。它按进程路径
验证 `UVNavi.exe`，按中英文菜单文字定位 `Tools -> Customize` 和
`Instrument -> Automatic Control`，并读取 Automation ID `9032` 的命令目录。界面显示 Waiting
只是必要条件；只有同一目录中的 Hello 反馈为 0 才返回 `READY`。

失败的 Hello 没有物理副作用，可以单独归档后重新校验。恢复标记只要不是 `Command=0`，运行时
管理器就拒绝清理、重试或继续测量。

### 持久化状态

```text
D:\UVVis-Automation\data\<batch_id>\batch-manifest.json
```

命令执行前先写入过渡状态，成功反馈和文件归档后再推进。超时、命令失败、原始数据缺失或导出
超时都会进入 `RECOVERY_REQUIRED`，不会自动重发 `Command=21` 或 `111`。

## 安全边界

MCP 是 AI tutor 与本地控制程序之间的结构化接口，不是 USB 驱动。执行工具只通过 LabSolutions
自动控制目录发送受审计命令，并且必须在方法存在、路径就绪、状态匹配、样品信息完整和操作员
授权后才能测量。批次启动、基线和每个样品动作还必须通过运行时 READY 门禁。当前执行状态机
开放 Spectrum 和 Photometric；Quantitation 与 Time Course 仍为只读规划。
