# VoicePet

VoicePet 是一个面向 Windows 的桌面语音助手。它以桌宠形式常驻桌面，支持中文唤醒、离线语音识别、兼容 OpenAI 协议的大模型服务、在线与本地语音合成，以及经过确认和审计的本地工具调用。

> 当前版本为 `0.1.0`，项目仍处于早期开发阶段

## 功能特性

- 桌面宠物与系统托盘交互
- 自定义中文唤醒词和唤醒灵敏度
- 基于 Sherpa-ONNX 的本地关键词检测
- 基于 faster-whisper 的本地中文语音识别
- 兼容 OpenAI Responses API 和 Chat Completions API
- Edge TTS 在线语音合成，并在不可用时回退到 Windows SAPI
- 本地短期会话、长期记忆和可控的数据保留策略
- 系统信息查询、文件移动及撤销等受限工具
- 敏感操作确认、操作审计和脱敏诊断包
- API Key 使用 Windows DPAPI 加密保存
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
python -m pip install -e ".[audio,asr,wake,llm,tts,tools,ui,build]"

python app.py
```

首次启动后：

1. 从系统托盘打开“设置”
2. 配置 API 地址、API Key 和模型名称
3. 按界面提示下载语音识别模型与中文唤醒模型
4. 保存需要重启才能生效的设置并重新启动 VoicePet

默认唤醒词是“你好，小蓝”。也可以从托盘菜单选择“开始聆听”，无需使用唤醒词。

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
│  └─ wake                中文唤醒模型
├─ cache                  语音播放等运行时临时缓存
├─ logs
│  └─ voicepet.jsonl      脱敏结构化日志
└─ pets                   用户安装的桌宠资源
```

faster-whisper 下载的模型也可能使用 Hugging Face 的系统缓存目录。删除模型后再次启动相关功能，可以重新测试下载确认流程。

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
