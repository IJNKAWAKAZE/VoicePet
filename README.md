# VoicePet

VoicePet 是一个面向 Windows 的桌面语音助手。它以桌宠形式常驻桌面，支持中文唤醒、离线语音识别、兼容 OpenAI 协议的大模型服务、在线与本地语音合成，以及经用户确认的 Agent 本机文件与命令操作。

> 当前版本 `0.1.0`，项目仍处于早期开发阶段，配置结构与本地数据格式仍可能随版本变化。

## 功能特性

**桌面交互**

- 桌宠常驻桌面，支持缩放、置顶、鼠标穿透与动态预览
- 内置 `kawakaze`（江风 Q版）原创形象，可从 Codex 宠物社区下载形象包导入
- 单击桌宠直接开始聆听，右键唤出桌宠快捷菜单，托盘菜单可随时暂停或恢复聆听
- 三套界面主题（晴海微风 / 深海夜航 / 樱花珊瑚）与“减少动态效果”开关
- 全局快捷键唤起聆听，默认 `Ctrl+Alt+Space`，无需先说出唤醒词
- 可设置开机启动，写入当前 Windows 用户的 Run 注册表项，不需要服务或管理员权限

**语音**

- 中文唤醒词默认“你好，小江”，可在设置中修改为 2 到 12 个中文字符并调整灵敏度
- 基于 Sherpa-ONNX 的本地关键词检测，模型缺失时在设置页提示并支持断点续传下载
- 连续对话：唤醒后可在设定的等待时间内连续说话，不必重复唤醒词
- 基于 faster-whisper 的本地语音识别，可选择 tiny / base / small / medium / large-v1 / large-v2 / large-v3 / large-v3-turbo
- 静音与转写幻觉过滤，唤醒监听异常自动恢复
- 语音合成优先使用 Edge TTS，失败时回退 Windows SAPI，支持试听与声音列表刷新

**对话**

- 使用 OpenAI Responses API，可自定义 Base URL
- 可调思考强度（最小 / 低 / 中 / 高 / 极高 / 最大）与 4000 字以内的角色设定
- 托盘常驻聊天窗口，文字与语音共享同一多轮上下文，消息按今天 / 昨天 / 具体日期标注时间
- 会话侧栏支持新建与切换会话，并显示每个会话最后一次消息时间；消息支持上传图片和文件
- 用户消息按原文纯文本显示，模型回复按 Markdown 渲染；单条消息上限 16000 字符，超限会在发送前提示
- 多个会话可以同时运行：只有当前显示的会话发声并驱动桌宠，其余会话在后台静默执行；切回仍在运行的会话时，桌宠会恢复该会话当前的执行状态，并能看到完整结果
- 长期记忆与近期摘要可在记忆页确认、编辑、删除、撤销与导出

**Agent**

- 对话统一由 Codex Agent 在独立 Worker 进程中处理，需要先在设置中配置 API Key
- 文件修改与命令执行使用 Codex SDK 的原生工具，由 Codex 执行器管理执行策略
- 可枚举窗口、截取窗口截图并按坐标点击与输入，用自然语言让 Agent 操作桌面应用
- Agent 需要确认时弹出“Agent 请求确认”窗口，是否逐步确认由聊天输入框的审批模式决定
- Codex Agent 会话线程可恢复，主进程只接收脱敏事件
- 聊天记录只保留对话本身：用户消息与模型回复（含模型自己交代的中间说明），思考、命令与工具输出只在桌宠气泡的执行状态里体现
- 回复按条目分段保存：重启或切换会话后仍是多条气泡，不会粘成一段；取消、中断或退出时已经显示过的内容同样会写入历史

**隐私**

- API Key 由 Windows DPAPI 加密保存，不写入 `config.json`
- 诊断包与结构化日志在写入前过滤凭据、提示词与其他敏感正文
- 诊断录音默认关闭，聊天与摘要保留天数可配置（1 到 365 天）

## 运行环境

- Windows 10 或 Windows 11，64 位
- Python 3.12
- 可用的麦克风与音频输出设备
- 支持 OpenAI Responses 协议的大模型服务与 API Key；自定义本机端点同样需要填写 Key

## 从源码运行

```powershell
git clone https://github.com/IJNKAWAKAZE/VoicePet.git
Set-Location VoicePet

python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[audio,asr,wake,llm,tts,ui,agent,build]"

python app.py
```

可选依赖按功能拆分，可以只安装需要的部分：

| 依赖组 | 作用 |
| --- | --- |
| `audio` | 麦克风采集与 WebRTC 语音活动检测 |
| `asr` | faster-whisper 本地语音识别与简繁转换 |
| `wake` | Sherpa-ONNX 中文唤醒 |
| `llm` | OpenAI 协议客户端 |
| `tts` | Edge TTS 与 Windows SAPI 回退 |
| `ui` | PySide6 与 QML 界面 |
| `agent` | Codex Agent Worker，配置 API Key 时必需 |
| `build` | PyInstaller 打包 |
| `dev` | 测试与静态检查（pytest、Pillow、ruff） |

## 首次配置

1. 从系统托盘打开“设置”
2. 在“AI 服务”页填写 API 地址与模型名称，输入 API Key 后点击“加密保存”
3. 在“语音与唤醒”页下载中文唤醒模型与语音识别模型，设置页会显示缺失、下载中与失败等状态
4. 切换语音识别模型后需要先保存并重启，再下载所选模型
5. 保存需要重启才生效的设置后重新启动 VoicePet

语音识别与唤醒模型都在本机使用，不会自动下载：

- 中文唤醒模型约 31.1 MiB，保存在 `%LOCALAPPDATA%\VoicePet\models\wake`
- faster-whisper `small` 模型约 464 MiB，保存在 `%LOCALAPPDATA%\VoicePet\models\asr\small`

## 使用方式

主窗口分为三个分区，左侧导航在窄窗口下自动收起为图标栏：

| 分区 | 内容 |
| --- | --- |
| 聊天 | 会话列表、语音与文字输入、图片与文件附件、Agent 模式切换、记忆变更撤销 |
| 记忆 | 长期记忆与近期摘要的查看、确认、编辑、删除、撤销与导出 |
| 设置 | 通用、AI 服务、语音与唤醒、桌宠、隐私、诊断、关于 |

托盘菜单提供打开 VoicePet、开始聆听（聆听中变为停止聆听）、显示或隐藏桌宠、恢复鼠标交互、手动输入、打开设置与退出 VoicePet。右键桌宠可以直接开始聆听、切换鼠标穿透或置顶。

设置页对应关系：

| 页面 | 主要设置 |
| --- | --- |
| 通用 | 主题、开机启动、减少动态效果、全局快捷键 |
| AI 服务 | API 地址、模型名称、思考强度、API Key、角色设定、连接测试 |
| 语音与唤醒 | 中文唤醒开关、连续对话与后续等待时间、唤醒词与灵敏度、识别模型下载、语音合成与试听 |
| 桌宠 | 形象列表与动态预览、导入形象包或目录、缩放、置顶、鼠标穿透 |
| 隐私 | 记忆与聊天历史开关、自动整理摘要、保留天数、诊断录音、导出长期记忆、清空会话历史 |
| 诊断 | 音频采集、语音识别、大模型、语音合成、数据库、桌宠资源与桌面自动化检查，导出脱敏诊断包 |
| 关于 | 版本信息与数据目录入口 |

记忆默认开启。关闭“使用记忆”后仍可在记忆页管理已存数据；清空会话历史只删除会话与相关摘要，保留长期记忆、设置和桌宠形象。

## 桌宠形象

内置形象为 `kawakaze`（江风 Q版），形象设定与图集由本项目制作，随项目按 Apache License 2.0 分发。旧版本内置的 `dpsk-girl`（鲸鱼娘）已移除：配置里选中该形象时会在加载时自动切换到江风，如果唤醒词仍是旧默认值“你好，小蓝”也会更新为“你好，小江”，自己改过的设置不受影响。

自定义形象目录需要一个 `pet.json`：

```json
{
  "id": "my-pet",
  "displayName": "我的桌宠",
  "description": "一句话描述这个形象",
  "spriteVersionNumber": 2,
  "spritesheetPath": "spritesheet.webp"
}
```

- `id` 需要匹配 `[a-z0-9][a-z0-9_-]{0,63}`，且不能与已有形象重名
- 图集为 8 列、每格 192 × 208 像素，整图宽度固定 1536；高度支持 1872（9 行，Codex 宠物社区通行格式）与 2288（11 行，完整 v2 布局，含 16 个环视方向）
- 行对应对待机、聆听、思考、执行等对话阶段，播放节奏由程序控制
- 可通过“导入形象包”（`.codex-pet` 或 `.zip`）或“导入目录”安装，形象保存在 `%LOCALAPPDATA%\VoicePet\pets`

导入会校验压缩包结构与像素规模：单个压缩包不超过 32 MiB，文件数不超过 32，解压后不超过 64 MiB，单张图片不超过 400 万像素。

需要更多形象可以浏览 Codex 宠物社区 `https://codex-pet.org/zh/`，站点上的 `.codex-pet` 宠物包下载后用“导入形象包”安装即可。

## Codex Agent

配置 API Key 并安装 `agent` 依赖后，对话统一由 Codex Agent 处理。聊天输入框右侧可以按会话切换审批模式，切换在下一轮生效：

| 模式 | 行为 |
| --- | --- |
| 建议模式 | 文件编辑和命令执行需要用户确认 |
| 自动编辑 | 原生补丁的文本创建与修改自动批准，Shell 命令仍需确认 |
| 全自动 | 原生工具按用户要求直接执行，不再弹出 VoicePet 二次确认 |

Agent 复用“AI 服务”页配置的 API 地址、模型名称、思考强度与角色设定，密钥同样由 Windows DPAPI 加密保存，不支持 ChatGPT OAuth。Agent 运行在独立 Worker 进程中，运行数据保存在 `%LOCALAPPDATA%\VoicePet\data\codex`；它的文件改动与命令执行在 Worker 内完成，不写入 VoicePet 的会话数据库。取消时会尝试关闭受管理的命令进程树，但已经完成的外部写入不会自动撤销。

Agent 通过 VoicePet 自带的 MCP 服务获得桌面能力：`list_windows` 枚举可见窗口，`get_window_state` 返回窗口或整屏截图，`click`、`scroll`、`drag`、`press_key`、`type_text` 完成鼠标与键盘操作。`click`、`drag` 的坐标相对目标窗口左上角，Agent 会先截图观察、再执行单个动作并重新截图确认。

输入文字默认走剪贴板粘贴，避免逐字注入被输入法串字；剪贴板粘贴失败时才退回逐字注入，并在粘贴后还原用户原来的剪贴板文本。

单轮操作的总时长由 `agent.max_turn_minutes` 决定（默认 30 分钟），超过后本轮会被中断并提示“本轮处理已达到资源上限”。

为避免语音误触，桌面操作和 `shutdown_computer`、`delete_file` 等不可逆工具只允许在**全自动**模式下执行：建议模式与自动编辑模式下调用会被拒绝并提示切换模式，只读的窗口枚举、截图以及文件、剪贴板、启动应用等工具不受限制。

## 数据与配置

运行数据默认保存在：

```text
%LOCALAPPDATA%\VoicePet
├─ config.json             应用配置，不包含 API Key
├─ data
│  ├─ assistant.db        会话、记忆与记忆变更数据
│  ├─ credentials.bin     由 Windows DPAPI 加密的 API Key
│  └─ codex               Codex Agent 线程与轮次记录
├─ models
│  ├─ asr
│  │  └─ <模型名>         faster-whisper 语音识别模型
│  └─ wake                中文唤醒模型
├─ cache                  语音播放等运行时临时缓存
├─ logs
│  └─ voicepet.jsonl      脱敏结构化日志
└─ pets                   用户安装的桌宠资源
```

`config.json` 带版本号，字段不完整、包含未知项或类型错误时会回退到默认设置并给出提示。旧版本配置里的默认唤醒词与已移除的内置形象会在加载时自动迁移，自定义过的设置不会被覆盖。通过设置下载的识别模型统一保存在 `models/asr/<模型名>`，检测和加载使用同一目录，下载中断后可以重试。旧 Hugging Face 缓存不再自动使用；迁移并验证模型可用后，可以清理对应旧模型缓存。缓存可能被其他软件共享，程序不会自动删除它。`pets` 仅在导入形象时创建，`models/wake` 仅在实际下载或安装时创建。

## 命令行参数

```powershell
python app.py --smoke-test          # 加载 QML 后立即退出，用于校验启动链路（冻结版会先校验打包资源与数据目录）
python app.py --reset-test-memory   # 确认后删除测试用的会话与记忆数据库
```

`--agent-worker` 由主进程内部使用，不建议手动调用。

## 测试

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
```

## 打包

在 PowerShell 中运行：

```powershell
.\packaging\build_release.ps1 -Python (Resolve-Path '.\venv\Scripts\python.exe').Path
```

打包结果位于：

```text
dist\VoicePet\VoicePet.exe
```

脚本会把 `LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES.md` 与 `packaging\licenses\LGPL-3.0.txt` 一并复制到 `dist\VoicePet`，许可证文本同时位于 `dist\VoicePet\_internal\licenses`。

可执行文件冒烟测试：

```powershell
.\dist\VoicePet\VoicePet.exe --smoke-test
```

## 项目结构

```text
app.py                      Qt 主进程与 Agent Worker 的统一入口
core/                       不含 Qt 的运行时：配置、音频、唤醒、识别、模型、记忆、Agent 与诊断
ui/                         PySide6 与 QML 界面层
  application.py            主进程装配、托盘与配置保存生命周期
  qml/                      Main.qml、components/、screens/、windows/
  viewmodels/               聊天、记忆、设置、桌宠、主题等 QML 交互状态
  tray.py                   系统托盘动作
  event_bridge.py           把运行时事件投递到 Qt 主线程
  global_hotkey.py          Windows 原生全局快捷键
  pet_animation.py          桌宠图集加载、切片与状态行映射
assets/                     品牌图标、主题素材与内置桌宠
packaging/                  打包脚本、PyInstaller hooks 与许可证文本
tests/                      core 与 QML 的分层测试
```

界面统一使用 QML，不保留旧 QWidget 窗口。`QApplication`、托盘与文件对话框仍依赖 QtWidgets，不能因旧窗口移除而删掉该依赖。

## 隐私说明

- 麦克风音频默认只在本地用于唤醒检测和语音识别
- 发送给大模型服务的只有转写文本，以及你在聊天中主动附加的图片与文件
- 诊断录音默认关闭
- API Key 不写入 `config.json`，而是绑定当前 Windows 用户加密保存
- 诊断包和结构化日志会过滤凭据、提示词及其他敏感正文
- 可在设置中关闭记忆、调整短期数据保留时间或清理本地数据

## 许可证

本项目以 Apache License 2.0 发布，完整条款见 `LICENSE`，署名信息见 `NOTICE`，第三方依赖及其许可证清单见 `THIRD_PARTY_NOTICES.md`。

分发包内含以下 LGPL-3.0 组件，它们以未修改的独立文件形式提供，可自行替换：

- `PySide6`（Qt for Python）
- `edge-tts`（其中 `src/edge_tts/srt_composer.py` 为 MIT 许可）
