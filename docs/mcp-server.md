# UV-Vis MCP 服务

`shimadzu-uvvis-mcp` 是供 AI tutor 调用的 stdio MCP 服务。当前只提供
`plan_uvvis_scan`。该工具只读取配置和检查文件路径，不会写入 `SPC_CMD.txt`、
连接仪器或开始测量。

## 安装与启动

```powershell
python -m pip install -e ".[mcp]"
shimadzu-uvvis-mcp --config C:\UVVis-Automation\control-pc.toml
```

也可以通过环境变量固定配置文件：

```powershell
$env:SHIMADZU_UVVIS_CONFIG = "C:\UVVis-Automation\control-pc.toml"
shimadzu-uvvis-mcp
```

MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "shimadzu-uvvis": {
      "command": "shimadzu-uvvis-mcp",
      "args": [
        "--config",
        "C:\\UVVis-Automation\\control-pc.toml"
      ]
    }
  }
}
```

## plan_uvvis_scan

结构化输入：

```json
{
  "start_nm": 400.0,
  "stop_nm": 700.0,
  "step_nm": 1.0,
  "direction": null,
  "profile_name": null
}
```

工具会调用公共 profile 解析器，返回：

- 规范化后的范围、步长和点数；
- 唯一匹配的 profile、`.vspm` 路径和扫描方向；
- 名义扫描时间（profile 登记了扫描速度时）；
- 方法文件、命令目录、数据目录和导出目录的就绪检查；
- `Command=0/100/110/111` 的只读命令计划模板；
- 明确的安全标记，表明本次调用没有执行物理操作。

没有匹配方法或存在多个匹配方法时，工具调用返回 MCP 错误。需要先在 LabSolutions
中创建并验证方法，然后登记到 `[scan_profiles.<name>]`；工具不会自动生成 `.vspm`。
