# 多样品顺序测量

## 设备约束

当前 UV-Vis 只有一个样品位和一个参比位，也没有已验收的自动进样器。因此多个不同样品不能
无人值守连续测量。参比池在整个批次中保持不变；每个样品开始前，操作人员必须更换样品池并
确认样品身份和放置位置。

如果实验步骤明确要求更换空白或参比，应拆成新的批次，不能在原批次中静默改变参比。

## AI tutor 和 MCP 流程

```text
AI tutor 解析实验步骤中的样品列表和测量参数
        ↓
调用 plan_uvvis_sample_batch
        ↓
获得固定顺序、每个样品的独立路径和人工门禁
        ↓
调用 start_uvvis_batch，准备已验证的 Spectrum 或 Photometric 方法
        ↓
Runtime Manager 自动校验目录、进入 Waiting，并完成 Hello
        ↓
提示操作人员放置空白，按方法执行一次基线校正
        ↓
提示操作人员放入 001_sample_a，并等待确认
        ↓
执行该样品的 LabSolutions 命令，等待数据和导出完成
        ↓
保存原始数据、归档导出文件和 manifest
        ↓
提示操作人员放入 002_sample_b，并等待新的确认
```

不同样品不能复用 Spectrum `series` 的定时自动重复流程。`series` 适用于同一样品的重复完整
光谱，不会在两次测量之间验证是否已经完成物理换样。

## 标识与目录

`batch_id` 和输入 `sample_id` 只允许 ASCII 字母、数字、下划线和连字符。批次规划按输入顺序
生成三位序号，并将序号加入实际发送给 LabSolutions 的 `SampleID`：

```text
D:\UVVis-Automation\data\experiment_20260716_001\
  001_sample_a\
    raw\
      001_sample_a.vspd
    export\
    plot\
      result.png
    manifest.json
  002_sample_b\
    raw\
      002_sample_b.vspd
    export\
    plot\
      result.png
    manifest.json
```

原始数据扩展名由模式决定：Spectrum `.vspd`、Photometric `.vphd`、Quantitation `.vqud`、
Time Course `.vtmd`。Photometric 超过 10 个波长时，同一个样品的 `raw` 目录包含多个分段
`.vphd`，但只需要一次样品放置确认。Photometric 执行器直接从每段 `.vphd` 生成标准 CSV；若原始文件结构无法识别，才回退到配置的公共导出目录。后续执行器必须
按唯一 `SampleID` 匹配完成的文件，再归档到该样品的 `export` 目录并生成 manifest。`plot` 目录
保存合并后的 `result.csv`、`result.json` 和 `result.png`，并同步到配置的仓库 `outputs` 目录。
Spectrum 优先直接解析 `.vspd`，无法识别时回退到 LabSolutions 自动导出 CSV，并要求完整匹配方法
中的范围、数据间隔和点数；Photometric 优先直接解析 `.vphd`。两种模式均在结果验证及发布成功后
才完成样品状态。

## 执行门禁

每个样品都必须独立满足以下条件：

1. 操作人员确认当前样品位中的样品与计划的 `SampleID` 一致。
2. 操作人员确认参比位仍是批次登记的参比。
3. 上一个样品的测量、原始数据保存和自动导出已经完成。
4. 目标样品目录和原始数据文件尚不存在。
5. 当前参数对应的方法文件已经在 LabSolutions 中生成并验证。
6. 批次开始前已按[吸光度空白与基线校正](absorbance-correction.md)完成空白校正，或由实验 SOP
   明确记录本批次可以沿用的有效基线。
7. `LabSolutionsRuntimeManager` 已确认命令目录一致、Automatic Control 处于 Waiting，且本次
   `Command=0` 返回 `Return=0`。基线和样品阶段只验证，禁止现场修改运行时配置。

任何一项不满足时都不能发送测量命令。`plan_uvvis_sample_batch` 是只读工具，只报告计划和冲突，
不会创建目录、写入命令文件、连接仪器或开始测量。Spectrum/Photometric 执行工具由持久化状态机约束，
完整接口、确认字段和恢复边界见 [UV-Vis MCP 服务](mcp-server.md)。
