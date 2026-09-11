# 第三方组件声明

VoicePet 以 Apache License 2.0 发布，完整条款见 `LICENSE`。程序运行时依赖以下第三方组件，它们各自遵循原始许可证，本文件仅作汇总说明。

## 直接依赖

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| PySide6 / Qt for Python | QML 界面 | LGPL-3.0-only（本项目按 LGPL-3.0 使用） |
| edge-tts | 在线语音合成 | LGPL-3.0（`src/edge_tts/srt_composer.py` 为 MIT） |
| faster-whisper | 本地中文语音识别 | MIT |
| sherpa-onnx | 中文唤醒词检测 | Apache-2.0 |
| openai | 大模型客户端 | Apache-2.0 |
| opencc-python-reimplemented | 简繁转换 | Apache-2.0 |
| pypinyin | 中文拼音处理 | MIT |
| sentencepiece | 分词 | Apache-2.0 |
| sounddevice | 音频采集与播放 | MIT |
| webrtcvad-wheels | 语音活动检测 | MIT |
| numpy | 数值计算 | BSD-3-Clause |
| pywin32 | Windows 系统集成 | PSF-2.0 |
| openai-codex | Codex Agent Worker | Apache-2.0 |
| PyInstaller | 打包工具（仅构建期使用） | GPL-2.0-or-later，含允许分发专有程序的 bootloader 例外 |

## LGPL-3.0 组件的合规说明

`PySide6` 与 `edge-tts` 以 LGPL-3.0 授权。本项目未修改这两个库，且以独立的动态库与模块文件形式随分发包提供，位于 `dist\VoicePet\_internal`，可自行替换为兼容版本，从而满足 LGPL-3.0 关于可替换性与可重新链接的要求。

如果计划修改这两个库或将其静态链接进本程序，需要同时公开相应的修改源码。

## 运行时下载的模型

语音识别与唤醒模型不随仓库或分发包提供，由用户在应用内按需下载：

- faster-whisper `small` 模型：源自 OpenAI Whisper，MIT
- 中文唤醒模型：由下载来源决定，详见对应模型仓库的许可证

## 许可证全文

`packaging\licenses\LGPL-3.0.txt` 保存了 LGPL-3.0 全文，随打包流程复制到 `dist\VoicePet`。

`PySide6` 6.11 的 wheel 只附带 Qt 商业许可说明，不包含 LGPL-3.0 原文，因此需要由本项目自行附带。其余许可证原文可从下列地址获取：

- LGPL-3.0：https://www.gnu.org/licenses/lgpl-3.0.txt
- Apache-2.0：https://www.apache.org/licenses/LICENSE-2.0.txt
- MIT、BSD-3-Clause、PSF-2.0、GPL-2.0：随对应组件发行包提供

## 维护说明

本文件与 `pyproject.toml` 的 `[project.optional-dependencies]` 保持一致。新增或移除依赖、或升级依赖的许可证版本时，请同步更新本文件。
