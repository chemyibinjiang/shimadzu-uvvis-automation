# 岛津 UV-Vis 连接方式来源记录

本文记录当前仓库采用 LabSolutions UV-Vis 文本交换方案的依据，便于后续与岛津工程师、实验室操作人员和软件开发人员对齐。

## 1. 已核对资料

### 1.1 微信沟通记录

来源为 2026-07-01 至 2026-07-02 的微信群沟通截图。

关键结论：

- 目标设备为岛津 UV-Vis 系列，现场型号需在控制电脑上最终确认，沟通中提到可能为 `UV-1900`、`UV-2600` 或 `UV-2700` 系列。
- 厂商沟通中未确认开放底层 SDK、API、串口命令或远程控制协议。
- 厂商确认当前仪器配套软件 LabSolutions UV-Vis 可通过文本交换方式完成上层软件对仪器测定、参数调用和结果输出的自动化。
- 命令文件需要保存到 LabSolutions 中配置的本地命令接收文件夹。LabSolutions 在自动控制模式下读取该文件夹中的命令文件。
- 一次命令文件只包含一条命令。上一条命令的反馈完成前，不应写入下一条命令。
- 光谱测定结果的原始数据主要保存为 LabSolutions 数据文件，例如 `.vspd`。CSV、TXT 或 Excel 结果不是通过自动控制命令直接 `EXPORT CSV`，而是要提前在 LabSolutions 软件内设置自动输出格式和目录。外部命令完成测定后，软件按预设自动生成这些结果文件。

### 1.2 自动控制手册

仓库内手册文件：

```text
docs\reference\LabSolutionsUV-VisAutoControl.pdf
```

原始来源文件：

```text
D:\Program Files\WX\xwechat_files\wxid_3wezudfrjgms22_d5e8\msg\attach\d960c21dcf2f502dfe1c0bfb8d098fd6\2026-07\Rec\449ab446407a1ba1\F\24\LabSolutionsUV-VisAutoControl.pdf
```

已核对元数据：

- 标题：`LabSolutions UV-Vis 自动控制功能说明书`
- 文档编号：`207-90527A`
- 生成日期：2020-05-14
- 页数：126
- SHA-256：`86E803B287145F6F451D7BCC455B8B7AA98EC34C6727D63A28C8C85423BAC2F4`

该 PDF 版权页声明未经许可不得复制部分或全部内容。当前副本按项目负责人确认，作为本仓库内部设备控制依据保存；对外分发仓库或发布 release 前应重新确认授权边界。

### 1.3 外部 Markdown 说明

外部文件：

```text
D:\Program Files\WX\xwechat_files\wxid_3wezudfrjgms22_d5e8\msg\file\2026-07\shimadzu_labsolutions_uvvis_operation.md
```

SHA-256：`920C8A3063A4D1DB16DD05EE82448291D5E848AB35F92E0069863B0A327BADAD`

仓库内文件：

```text
docs\labsolutions-operation.md
```

SHA-256：`CF713062807A56E4DDA7F3E8209828C375182774E27F826C614BF798ADDC04B4`

结论：两者不一样。外部 Markdown 是英文长版操作说明；仓库内文档是中文交接说明，已经结合本项目脚本、现场验收流程、波长控制和时间步长边界重新整理。

## 2. 手册事实摘录

以下为已核对事实的摘要，不替代原手册。

| 手册位置 | 已核对事实 | 本项目处理 |
| --- | --- | --- |
| 前言使用注意事项 | LabSolutions UV-Vis 不支持 Unicode；数值输入必须使用半角数字。 | 首次现场验收要求路径、样品名、SampleID 和文件名使用 ASCII。 |
| 1.1 | 自动控制通过上层系统与 LabSolutions 交换命令文件和反馈文件实现。 | 本项目只作为 LabSolutions 文本交换客户端，不直接控制 USB、串口或底层驱动。 |
| 1.2 | 上层系统保存命令文件到命令接收文件夹；LabSolutions 读取后删除命令文件、执行命令、写反馈文件。 | 客户端先写临时文件，再原子替换为正式命令文件，并等待匹配反馈。 |
| 1.3 | 一个命令文件只能记载一条命令；执行命令时无法接收下一条命令；反馈文件发出后才接收下一条。 | 客户端对高层流程加进程间锁，避免并发写入同一命令目录。 |
| 1.3 | Spectrum 使用 `SPC_CMD.txt` 和 `SPC_RES.txt`；Quantitation 使用 `QUA_CMD.txt` 和 `QUA_RES.txt`；Photometric 使用 `PHO_CMD.txt` 和 `PHO_RES.txt`；Time Course 使用 `TMC_CMD.txt` 和 `TMC_RES.txt`。 | 四模式 MCP 请求和命令计划已实现；真实执行目前只开放 Spectrum。 |
| 1.4-1.6 | 命令文件第一行是 `Command=<编号>`，后续行为参数；反馈文件包含 `Command`、`Return`、`Error`；命令和反馈文件使用 UTF-8。 | 文件编码固定为 UTF-8，但现场文本内容仍按 ASCII 保守执行。 |
| 5.8 | `Command=21` 可按方法自动校正、范围基线校正或单波长调零。 | 首次真实测量默认不自动校正，只有操作人员确认后才启用。 |
| 5.11.1 | `Command=100` 加载 Spectrum 参数文件；参数文件必须与登记机型匹配。 | 配置文件登记 `.vspm`，现场验收要求人工核对波长范围和扫描参数。 |
| 5.11.2 | `Command=110` 设置 Spectrum 样品信息、数据文件名、SampleID 等。 | 本项目统一使用 run ID 关联样品、`.vspd`、导出文件和审计记录。 |
| 5.11.3 | `Command=111` 执行 Spectrum 测定；执行前需连接仪器并加载参数文件；多联池和抽吸附件需先初始化。 | 首次测试强制 `MeasurementMode=2`、`Discharge=OFF`，避免误测全部池位或触发排出。 |
| 5.11.3 | `MeasurementMode=1` 测定所有已配置样品池，`MeasurementMode=2` 只测当前样品池；无效或缺省时可能按 `1` 处理。 | 命令中显式写入 `MeasurementMode=2`。 |
| 5.11.3 | `Discharge=ON/OFF` 用于抽吸或注射式抽吸单元；无效或缺省时可能按启用处理。 | 命令中显式写入 `Discharge=OFF`。 |
| 5.11.3 | 常见测量错误包括未加载参数文件、多联池未初始化、测定错误、人工中止、数据文件名无效、同名数据文件已加载、光谱数据保存失败。 | 文档和代码均要求负 `Return` 时停止流程，保留反馈和审计，不无限重试测量命令。 |
| 5.14 | 手册示例使用 `.vtcm` 和 `Command=400/410/411`；测定波长、间隔和总时长仍由方法文件定义。 | 本机 1.13 程序组件登记 `.vtmm/.vtmd`；配置暂时接受 `.vtmm` 和 `.vtcm`，现场以“另存为”结果为准。 |

## 3. 当前软件边界

- 连接仪器由 LabSolutions 完成，本项目只发送 `Command=1` 等文本命令。
- 测量参数由 `.vspm`、`.vphm`、`.vqum` 或 `.vtmm/.vtcm` 方法文件保存，本项目不解析这些专有文件。
- `Command=111` 不提供起始波长、终止波长、步长、扫描速度或每点等待时间参数。
- CSV、TXT、Excel 输出由 LabSolutions 自动输出设置决定。本项目只等待导出目录中新文件稳定后记录大小和 SHA-256。
- 命令目录、数据目录、导出目录和日志目录首次建议使用 ASCII 路径。
- 与自动进样、抽吸、排出、清洗、多联池、反应器联动和长期无人值守相关的动作，必须在真实控制电脑和仪器上逐项验收后再开放。
