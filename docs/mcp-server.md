# UV-Vis MCP 服务

`shimadzu-uvvis-mcp` 是供 AI tutor 调用的 stdio MCP 服务。当前提供两个只读工具：

- `plan_uvvis_measurement`：统一规划 Spectrum、Photometric、Quantitation 和 Time Course。
- `plan_uvvis_scan`：兼容旧调用，精确匹配已登记的 Spectrum profile。

两个工具都不会写入 `*_CMD.txt`、编辑方法文件、连接仪器或开始测量。

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
| `mode` | 全部 | `spectrum/photometric/quantitation/time_course` |
| `signal_type` | 全部 | 默认 `absorbance`，必须匹配登记模板 |
| `template_name` | 全部 | 有多个同类模板时显式指定 |
| `start_nm/stop_nm/step_nm` | Spectrum | 连续扫描范围和数据间隔 |
| `direction` | Spectrum | 可选 `ascending/descending` |
| `wavelengths_nm` | Photometric | 离散波长数组 |
| `wavelength_nm` | Quantitation、Time Course | 测定波长 |
| `interval_seconds/duration_seconds` | Time Course | 采样间隔和总时长 |

示例：

```json
{
  "mode": "time_course",
  "wavelength_nm": 520,
  "interval_seconds": 1,
  "duration_seconds": 600
}
```

结果包括规范化请求、模板、目标方法文件、数据扩展名、路径就绪检查和模式对应的命令计划。
目标方法不存在时返回：

```json
{
  "status": "method_generation_required",
  "plan_only": true,
  "method_generation": {
    "required": true,
    "automatic_generation_supported": false
  }
}
```

这不是错误地把模板直接执行，而是明确告诉 AI tutor：必须先在 LabSolutions 中生成并验证该参数
组合的方法。完整说明见 [四种测量模式与方法模板](four-mode-methods.md)。

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

## 安全边界

MCP 是 AI tutor 与本地控制程序之间的结构化接口，不是 USB 驱动。未来的执行工具也只能通过
LabSolutions 自动控制目录发送受审计命令，并且必须在方法存在、路径就绪、样品信息完整和操作员
授权后才能测量。
