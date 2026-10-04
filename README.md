# Codex 上下文指示器

Windows 上的独立底部浮层，外观参考 DeepSeek Harness 的圆环图标。显示「圆环 + 使用百分比 + 当前对话名称」，例如「添加 Codex 上下文用量提醒」。跟随前台 Codex / ChatGPT 桌面窗口的底边显示；切到其他应用或最小化窗口时隐藏。悬停查看简要用量，点击查看详情、按名称切换对话、调整阈值、复制交接文档提示词。

这是伴随工具，**不是 Codex 输入框的原生组件**。位置依据窗口底边计算，不能识别输入框；打开侧栏、调整布局时可拖动指示器重新定位。自动读取 Codex 桌面导航日志，切换到哪个对话就显示哪个对话的用量；后台其他对话生成消息不会改变监控对象。

## 启动

需要 Python 3.10+ 和 Tkinter（Windows 官方 Python 安装通常已包含），无需第三方依赖或 API Key。

下载仓库 ZIP 并解压，或者使用 Git：

```powershell
git clone https://github.com/xiaoPhaif4a2/codex-context-usage.git
cd codex-context-usage
```

在项目目录执行：

```powershell
python -m context_indicator
```

也可以双击 `start.pyw`，无终端窗口启动（需已关联 Python `.pyw` 文件）。

或者：

```powershell
.\start.ps1
```

启动后将 Codex 窗口置于前台，在窗口底部约 74% 宽度处出现圆环。可拖动调整位置，右键选择「恢复底部位置」或「退出指示器」。只有浮层匹配到前台 Codex.exe / ChatGPT.exe 窗口时才显示，进程名称不同的桌面版本需调整 `context_indicator/windows.py` 的匹配列表。

每个项目只允许一个浮层实例，重复启动会提示已在运行。

默认跟随 Codex 当前页面，支持跨项目切换。名称从本地 `state_*.sqlite` 的命名元数据读取，`session_index.jsonl` 作为备用。底部、悬停说明和详情均显示名称；详情下拉列表显示「名称 · 使用百分比」。同名对话附带项目和序号区分，不展示 UUID。下拉选择会通过 `codex://threads/...` 打开对应 Codex 对话，并等待实际页面导航信号再更新用量。

旧版保存的会话 ID 和环境变量 `CODEX_THREAD_ID` 不会将指示器固定到启动对话。位置和阈值仍保存在项目 `.context-indicator.json`，不会修改 Codex 配置。

仅列出某个项目的对话（其他项目页面会显示未知）：

```powershell
python -m context_indicator --project C:\path\to\project --project-only
```

初始阈值默认 70% 提醒、85% 强提醒，可通过详情窗口修改，或首次启动时设置：

```powershell
python -m context_indicator --warning 65 --critical 80
```

已保存的项目阈值优先于命令行初始值。每个会话每个等级在本次运行中提醒一次，避免每次刷新弹出。使用量降至提醒阈值以下 3 个百分点，或发生上下文压缩后，重新允许提醒。提醒仅在 Codex 窗口处于前台时显示。

预览外观（明确使用示例数据）：

```powershell
python -m context_indicator --demo
```

也保留可选的浏览器面板：

```powershell
python -m context_indicator --web --open
```

该面板默认地址 `http://127.0.0.1:8765`。端口占用时可添加 `--port 0` 自动分配。按 Ctrl+C 停止服务；浏览器面板阈值保存在浏览器中，独立于浮层设置。

## 交接流程

1. 指示器达到阈值，圆环变黄 / 红，并显示一次交接提醒。
2. 点击「复制提示词：请 Codex 生成交接文档」。
3. 粘贴到显示名称对应的当前对话并发送，请 Codex 根据实际工作生成项目根目录 `HANDOFF.md`。
4. 确认文档已生成后，新建对话并输入：`请读取 HANDOFF.md，按其中的目标、约束和剩余事项继续工作。`

工具只提供生成文档的提示词；不自动发送消息或创建新对话。切换对话只发生在用户主动选择下拉列表时。

## 数据口径与限制

读取 `${CODEX_HOME}/sessions/**/*.jsonl`，默认 `~/.codex/sessions`，只保留主对话元数据及 token 统计。浮层每 0.5 秒检查，增量读取日志，等待不完整 JSON 行写完后再处理。子代理、归档目录和云端会话不纳入用量显示；默认包含所有项目。日志读取过程中经过会话内容，但不保留或展示消息、工具输出，也不访问 `auth.json`。

自动跟随使用 `%LOCALAPPDATA%/Codex/Logs` 中的桌面主进程日志（自动兼容 Store 的 LocalCache 目录）。只提取 `browser sidebar owner sync` 事件的 `ownerRoutePath`、窗口 / 进程编号和时间；后台 App Server 的 `conversationId` 不作为页面选择依据。可用 `--desktop-logs C:\path\to\Logs` 指定目录。没有有效导航信号时显示未知，不回退到最近生成消息的对话。多个主窗口无法确定焦点时也显示未知。

```text
使用比例 = last_token_usage.total_tokens / model_context_window × 100%
```

最近一次调用的输入与输出总量近似反映上下文占用；这不是模型此刻全部内部状态的精确计量。缓存输入仍占上下文，不能扣除；`total_token_usage` 是累计消耗，不能当占用率。采用日志报告的窗口大小，不写死模型窗口。缺少有效用量 / 窗口时显示未知；压缩或模型切换后等待新读数。窗口在长回复期间可能只报告最近一次完成请求，页面切换本身也不产生 token 读数。

本地 JSONL 和桌面导航日志格式是观察到的实现细节，未来版本可能变化。已在本机 26.930 桌面版的真实导航事件上核对。官方 App Server 文档提供 `thread/tokenUsage/updated` 事件，但本工具没有接管桌面程序的 App Server 连接，使用现有日志作为数据源。

参考：[App Server 用量事件](https://learn.chatgpt.com/docs/app-server)、[终端状态栏配置](https://learn.chatgpt.com/docs/config-file/config-sample)、[MCP 自定义 UI](https://developers.openai.com/plugins/build/chatgpt-ui)。

## 验证

```powershell
python -m unittest discover -s tests -v
```

覆盖当前页面跟随、跨项目切换、后台生成不影响选择、显示真实名称、同名区分、名称更新、累计消耗与当前占用区分、阈值边界、缓存、缺失数据、半行写入、损坏行、日志截断、模型切换、压缩恢复、项目 / 子代理过滤、重复提醒、HTTP 本地访问限制。多屏 DPI 还应在目标桌面版本上验证。
