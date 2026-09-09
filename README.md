# VoicePet

VoicePet 是一个面向 Windows 的桌面语音助手。它以桌宠形式常驻桌面，支持中文唤醒、离线语音识别、兼容 OpenAI 协议的大模型服务、在线与本地语音合成，以及经过确认和审计的本地工具调用。

> 当前版本为 `0.1.0`，项目仍处于早期开发阶段

## 功能特性

- 桌面宠物与系统托盘交互
- 托盘常驻聊天窗口，支持文字与语音共享多轮上下文
- 自定义中文唤醒词和唤醒灵敏度
- 基于 Sherpa-ONNX 的本地关键词检测
- 基于 faster-whisper 的本地中文语音识别
- 兼容 OpenAI Responses API 和 Chat Completions API
- Edge TTS 在线语音合成，并在不可用时回退到 Windows SAPI
- 本地短期会话、长期记忆和可控的数据保留策略
- 系统信息查询、文件移动及撤销等受限工具
- 敏感操作确认、操作审计和脱敏诊断包
- API Key 使用 Windows DPAPI 加密保存`r`n- 可选的 Codex Agent Worker，支持会话线程恢复和三档执行模式
- 唤醒监听异常自动恢复和静音转写幻觉过滤

## 运行环境

- Windows 10 或 Windows 11，64 位
- Python 3.12
- 可用的麦克风与音频输出设备
- 支持 OpenAI 协议的大模型服务及其 API Key，或无需 API Key 的本地兼容服务

语音识别和唤醒模型均在用户确认后下载：

- faster-whisper `small` 模型约 464 MiB
- 中文唤醒模型约 31.1 MiB

## 从源码运行

```powershell
git clone https://github.com/IJNKAWAKAZE/VoicePet.git
Set-Location VoicePet

python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[audio,asr,wake,llm,tts,tools,ui,agent,build]"

python app.py
```

首次启动后：

1. 从系统托盘打开“设置”
2. 配置 API 地址、API Key 和模型名称
3. 按界面提示下载语音识别模型与中文唤醒模型
4. 保存需要重启才能生效的设置并重新启动 VoicePet

默认唤醒词是“你好，小蓝”。也可以从托盘菜单选择“开始聆听”，无需使用唤醒词。托盘中的“手动输入”会打开常驻聊天窗口，文字和语音消息都进入当前会话。

Codex Agent 使用官方 OpenAI API Key，密钥仍由 Windows DPAPI 加密保存，不支持 ChatGPT OAuth。设置页可以启用 Agent 并选择默认模式，聊天输入框可以为当前会话覆盖模式：建议模式逐次确认，自动编辑自动接受普通文本创建和补丁，全自动使用完整访问并跳过 VoicePet 二次确认。模式切换在下一轮生效，Windows 用户权限和 UAC 仍然有效。

Agent 运行数据保存在 %LOCALAPPDATA%\\VoicePet\\data\\codex。Worker 是独立进程，取消时会尝试关闭受管理的命令进程树；已经完成的外部写入不会自动撤销。锁定 runtime 尚未证明执行前审批能力时，建议模式和自动编辑会显示不可用，避免误把全自动行为当成逐次审批。

在“设置 → 语音”中，如果已启用中文唤醒但缺少模型，开关下方会持续提醒，并提供手动下载按钮。进入页面、开启开关及下载完成后会检查本地模型；不会因此自动下载或弹窗。`models/wake` 仅在实际下载或安装时创建，已有空目录不代表模型已下载。

设置中的“会话”页面可以查看完整多轮记录、新建会话或切换当前会话。切换会话只改变短期聊天上下文，不影响已经确认的长期记忆。

## 配置与本地数据

运行数据默认保存在：

```text
%LOCALAPPDATA%\VoicePet
```

主要内容包括：

```text
VoicePet
├─ config.json             应用配置，不包含 API Key
├─ data
│  ├─ assistant.db        会话、记忆和审计数据
│  └─ credentials.bin     由 Windows DPAPI 加密的 API Key
├─ models
│  ├─ asr
│  │  └─ <模型名>         faster-whisper 语音识别模型
│  └─ wake                中文唤醒模型
├─ cache                  语音播放等运行时临时缓存
├─ logs
│  └─ voicepet.jsonl      脱敏结构化日志
└─ pets                   用户安装的桌宠资源
```

通过设置下载的 ASR 模型统一保存在 `models/asr/<模型名>`，检测和加载使用同一目录，下载中断后可重试。旧 Hugging Face 缓存不再自动使用；迁移并验证模型可用后，可以清理对应旧模型缓存。缓存可能被其他软件共享，程序不会自动删除它。

## UI 结构

界面统一使用 QML，不再保留旧 QWidget 窗口或旧界面切换入口：

- `ui/application.py`：主进程装配与启动入口
- `ui/qml_application.py`、`ui/qml/`、`ui/viewmodels/`：QML 窗口编排、页面和交互状态
- `ui/pet_catalog.py`：宠物候选发现与资源目录解析
- `ui/pet_animation.py`、`ui/pet_animation_model.py`：图集加载与 QML 动画状态

托盘、事件桥、全局快捷键和 Markdown 清理继续复用独立模块。`QApplication`、托盘及文件对话框仍依赖 QtWidgets，不能因旧窗口移除而删掉该依赖。

## 测试

```powershell
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

可执行文件冒烟测试：

```powershell
.\dist\VoicePet\VoicePet.exe --smoke-test
```

## 隐私说明

- 麦克风音频默认只在本地用于唤醒检测和语音识别
- 只有转写后的文本会按配置发送给大模型服务
- 诊断录音默认关闭
- API Key 不写入 `config.json`，而是绑定当前 Windows 用户加密保存
- 诊断包和结构化日志会过滤凭据、提示词及其他敏感正文
- 可在设置中关闭记忆、调整短期数据保留时间或清理本地数据

## 许可证

本仓库目前尚未添加许可证文件。在许可证明确之前，默认保留所有权利。

