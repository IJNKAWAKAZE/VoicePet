# VoicePet Codex Agent 集成设计

## 目标

将 VoicePet 的聊天运行时升级为基于 OpenAI Codex SDK 的本地电脑 Agent。Agent 能处理普通闲聊，也能通过多轮规划和工具调用完成代码、文件、命令及系统操作任务。用户使用自己的 OpenAI API Key，VoicePet 提供三档可切换的审批模式。

本文记录已确认的产品行为和拟实施的协议，不表示 SDK 集成或 Windows 实机验证已经完成。审批模式的技术验证是实施计划的第一项，验证标准见下文。

## 产品边界

- Agent 访问范围覆盖当前 Windows 用户有权限访问的整台电脑，不限制在单一项目目录。
- `full_access` 不代表绕过 Windows 用户权限或 UAC；需要管理员权限的操作仍由 Windows 处理。
- 不接入 ChatGPT OAuth、Codex 订阅登录或其他账号认证方式。
- API Key 继续由 VoicePet 使用 Windows DPAPI 加密保存，普通配置文件不保存原文。

## 总体架构

```text
QML 聊天界面与语音层
        ↓
Coordinator / Agent Adapter
        ↓ 本地 JSON-RPC / stdio
Codex Agent Worker（独立进程）
        ↓
openai-codex Python SDK
        ↓
Codex app-server 与系统工具
```

### Agent Adapter

Adapter 位于现有 Coordinator 与 Worker 之间，负责把 VoicePet 用户输入转换为 Codex turn，把 Codex 事件转换为 VoicePet 内部事件，并为每个 turn 固化审批模式快照。QML 不直接依赖 Codex SDK 或 app-server 协议。

### Codex Agent Worker

Worker 是独立 Python 进程，负责初始化 Codex SDK、创建或恢复 thread、运行 turn、转发流式事件、处理取消和上报错误。SDK 或 app-server 协议变化只影响 Worker。

首期全应用同时运行一个 Agent turn；切换会话不改变运行中任务的归属。另一个会话可以浏览历史，但开始新任务前需结束或取消当前任务。独立进程提供故障隔离，不是权限沙箱。

SDK 的同步接口不得阻塞 Qt 主线程，采用异步客户端。Worker 内优先通过 Python SDK 访问 app-server；如选定版本的封装未暴露所需协议能力，只在 Worker 内补充 app-server 协议适配。

### 会话映射

每个 VoicePet 会话保存一个 `codex_thread_id`。每次用户输入创建一个 `turn_id`，并保存使用的 `approval_mode` 快照。VoicePet 数据库继续作为 UI 消息、语音记录和应用级归档的来源；Codex thread 负责 Agent 上下文。

VoicePet turn ID 和 Codex turn ID 分开保存，不能假定两者相等。已绑定 thread 的后续输入不再次完整重放 VoicePet 历史；旧会话首次接入时传入受长度限制的会话摘要和最近消息。沿用现有长期记忆召回与提取服务，按轮注入相关记忆，不让两套短期摘要互相重复压缩。

文本和语音进入同一个 Adapter。图片保留类型和路径，其他附件提供元数据供 Agent 按需读取。语音只播报面向用户的回复，不朗读命令输出、文件差异或推理事件。用户取消后，迟到事件不得触发下一轮语音播放。

## 审批模式

模式可在设置页修改，也可通过聊天输入框下方工具栏即时切换。设置页保存全局默认值；工具栏保存当前会话的覆盖值，不改全局设置，支持恢复使用默认值。新会话默认继承全局值；首次安装及旧配置迁移默认使用自动编辑。正在运行的 turn 使用启动时快照，模式切换从下一轮生效；运行时切换须显示“下一轮生效”。

| 模式 | 文件读取 | 普通文件修改 | 命令执行 | 删除、覆盖、安装、系统设置 |
|---|---|---|---|---|
| 建议模式 | 自动 | 逐次确认 | 逐次确认 | 逐次确认 |
| 自动编辑 | 自动 | 整台电脑自动执行 | 逐次确认 | 逐次确认 |
| 全自动 | 自动 | 自动执行 | 自动执行 | 自动执行 |

全自动模式使用 Codex `full_access` 和自动批准策略，不经过 VoicePet 的二次审批。执行仍受 Codex 运行时、当前 Windows 用户权限和 UAC 限制。

普通文件修改包含创建文件，以及通过可展示差异的补丁更新已有文件；这种正常编辑虽会改变原文件，不归入表中的“覆盖”。“覆盖”指以另一个文件替换目标文件、清空或无法提供差异的整体替换。删除和覆盖需要确认的规则仅适用于前两档。执行命令可能包含复合操作，前两档对整个命令审批，不承诺能静态识别每个内部动作。

### Codex 参数映射与验证边界

沙箱决定操作范围，审批策略决定哪些操作暂停；两者必须独立配置。所有模式的产品目标均包含全电脑访问，不能把前两档静默缩成工作区模式。

- 全自动的候选配置为 Python `Sandbox.full_access` 与 `approvalPolicy: never`；app-server 对应的 turn 沙箱类型为 `dangerFullAccess`。按锁定版本生成的协议验证字段和实际生效配置。
- 前两档必须能在执行之前接收命令和文件变更请求。建议模式转发到 UI；自动编辑仅对可检查的普通文件创建/补丁返回单次批准，其余请求转发 UI。不得使用 `acceptForSession` 或长期目录授权替代逐次判定。
- 官方文档说明审批请求“取决于配置”，并未保证在任意 full-access 配置下所有操作都会发出审批请求。`item/started` 和 `item/completed` 是观察事件，不能用它们在操作发生后补做审批。
- 实施第一步在可销毁测试目录和假命令上验证 SDK 是否能完整提供执行前拦截，包括普通补丁、删除、替换、命令和脚本启动。单纯调整提示词或仅设置 `on-request` 不算满足逐次审批。
- 如原生参数无法保证前两档语义，使用经过验证的执行前 hook 或受控通用执行入口；需要禁止可绕过入口的内置执行路径。若锁定版本无法提供这种拦截，则该模式应标为不可用并报告能力缺口，不能显示为建议/自动编辑却按全自动运行。

该验证不会改变已确认的产品行为，只用于确定实现机制。全自动不因上述前两档的验证要求增加 VoicePet 二次确认。

## Worker RPC 与事件

复用现有 Worker 的匿名管道启动和有界消息设计，新增独立 Agent 协议。现有 `core/tool_rpc.py` 仅允许 `execute/cancel/ping`，不能直接承载 Agent 通知和双向审批。

传输采用 UTF-8、每行一个 JSON 对象。请求使用 JSON-RPC 2.0 的 `id`，响应回显 `id`；通知没有 `id`。每个任务事件携带 `session_id`、VoicePet `turn_id`、单调递增 `seq`，以及可为空的 Codex `thread_id`、`codex_turn_id`、`item_id`。stdout 只输出协议数据。单条消息不超过 1 MiB，超长文本分块，有界队列必须优先保证取消与审批响应不会被输出流阻塞。

核心请求：

```json
{
  "jsonrpc": "2.0",
  "id": "request-123",
  "method": "agent.turn.start",
  "params": {
    "session_id": "session-123",
    "thread_id": "thread-456",
    "turn_id": "turn-789",
    "input": "帮我检查电脑上的项目并修复测试失败",
    "approval_mode": "auto_edit"
  }
}
```

取消请求：

```json
{
  "jsonrpc": "2.0",
  "id": "request-124",
  "method": "agent.turn.cancel",
  "params": {
    "turn_id": "turn-789",
    "reason": "user_cancelled"
  }
}
```

`agent.initialize` 先协商协议版本并返回 SDK/runtime 版本和可用能力，再接受任务。`agent.turn.start` 先响应接受结果，最终状态通过事件返回；重复的 turn ID 不得再次执行。新会话的 `thread_id` 为 null，由 Worker 创建并上报绑定，主进程持久化后才能继续依赖该绑定。

审批由 Worker 发出 `approval_request` 通知，携带唯一 `approval_id`、Codex 原始请求关联、操作类别、命令或差异及允许的决定。主进程通过 `agent.approval.resolve` 提交 `approval_id` 和 `accept/decline/cancel`。Worker 验证归属、模式快照和待决状态后只消费一次；过期、重复或其他会话的响应无效。取消任务同时清理待决审批。

另提供 `agent.shutdown` 用于有序退出。RPC 异常只报告有界错误码和安全消息，不附带原始请求体。

Worker 事件统一为 `agent.event`，事件类型至少包括：

- `text_delta`
- `command_started`
- `command_completed`
- `command_output_delta`
- `file_change`
- `tool_started` / `tool_completed`
- `plan_updated`
- `usage_updated`
- `approval_request`
- `turn_completed`
- `cancelled`
- `error`

事件保留 `turn_id`、时间戳和脱敏摘要。命令参数、文件正文、API Key 和凭据不得进入普通日志或诊断包。

文件变更事件区分 proposed/completed/failed/declined，提议不等于实际写入。终态以 `turn_completed` 的 completed/cancelled/failed 为准，只保存一次；错误事件本身不重复归档。断线且执行结果不明时使用 outcome_unknown，不自动假定执行失败。

聊天界面和任务详情可以显示用户需要审阅的命令、差异和限长输出；它们不进入审计摘要。Codex 运行时自身可能持久化原始会话或工具内容，这不受 VoicePet 的日志脱敏自动覆盖，必须纳入本地数据管理范围。

## 认证与凭据

主进程从现有 DPAPI 凭据存储读取 OpenAI API Key，通过不记录正文的匿名启动管道交给 Worker，使用 SDK 支持的 API-key 注入方式。凭据引用本身不能完成认证，不在每个 turn 中重复传 Key，也不放入进程命令行。

Codex 运行数据使用 VoicePet 自己的目录 `%LOCALAPPDATA%\VoicePet\data\codex`，不继承用户独立 Codex 安装的登录态或全自动配置。若运行时需要保存认证，必须先验证其存储方式，保持“普通配置不保存明文 Key”的要求。API Key 只供 runtime 上游认证，验证模型启动的命令子进程不会继承该秘密；不能假定退出进程等于可靠清零所有内存。

首期以官方 OpenAI API 为认证和兼容验收目标。保留旧后端配置，不把任意 Chat Completions 兼容端点自动视为 Codex 兼容端点。模型仍由设置选择，只提供在选定运行时可验证的模型能力。更换 Key 或模型在下一轮生效，不在任务执行中重建认证。

## 错误、取消与恢复

- Worker 在接受任务前启动失败时，界面显示后端不可用，可以明确切换到现有纯聊天后端；降级后不宣称具有 Codex 工具能力。任务被接受后不得因错误自动重放到旧后端。
- Worker 异常时保留已经收到的文本和事件，并允许重启 Worker 后恢复同一 Codex thread。
- 用户取消先发送 `agent.turn.cancel` 并映射到 Codex 的 turn 中断；5 秒未完成则关闭 SDK/runtime，额外 2 秒后终止受管理的进程树。Windows 使用 Job Object 或经验证的等效机制管理 Worker、app-server 及命令后代，不能仅杀 Worker PID 就宣称命令已停止。
- 应用关闭先取消活动 turn，再等待 Worker 有限时间退出。
- 单个 turn 的超时、取消和错误不得破坏其他会话。

首次运行时初始化允许 30 秒并显示启动状态。复杂 Agent turn 不沿用现有 4 次工具调用与 120 秒预算；独立配置默认 30 分钟运行预算，用户可以调高，等待用户审批期间不消耗运行预算。流式进度与取消必须在预算期间可用。

崩溃后先查询 thread 状态，再允许用户继续；不得自动重试可能已执行的命令或重复创建 turn。恢复后的上下文可能已包含部分副作用，需要显示结果未知。取消不撤销已经完成的写入、安装或外部操作，首期不承诺通用撤销。应用退出后的进程树清理需 Windows 实测，刻意脱离管理的外部服务不算可以自动撤回的任务。

## 数据迁移

新增 `agent_session_bindings` 表保存 VoicePet session ID、Codex thread ID、运行时实例标识和可为空的会话模式覆盖。新增 `agent_turns` 表保存 VoicePet turn ID、Codex turn ID、模式快照、开始与结束时间、最终状态、使用量及脱敏摘要；新增 `agent_events` 表按 turn ID 与 seq 去重保存事件摘要。迁移通过现有 schema 版本机制事务执行，不修改旧会话正文。

旧会话没有绑定时惰性创建。删除会话和清理本地数据同时覆盖绑定、事件摘要及 VoicePet 独立 Codex 数据目录内对应的会话记录；不得删除用户其他 Codex 安装的数据。运行中的会话先停止再删除，远端模型请求不视为能由本地清理撤回。

## 依赖与交付

锁定 `openai-codex` 和配套 runtime 的兼容版本，记录协议能力；不依赖用户系统 PATH 中任意版本的 Codex。官方说明 Python SDK 发布包含固定 runtime 依赖，但 Windows x64、Python 3.12 和 PyInstaller 产物仍需实际验证。发布包应包含运行所需文件，缺失时给出明确诊断，不让用户发送第一条消息后才无提示安装依赖。

现有工具 RPC 保持独立，新模块建议为 `agent_types`、`agent_rpc`、`agent_client`、`agent_process`、`agent_worker`、`agent_codex_adapter`、`agent_approval` 与 `agent_store`。Coordinator 只负责路由和事件协调；工具栏沿用现有 QML 控件样式。首期验证 Codex 文件和命令能力，扩展事件预留给 MCP，图形化控制任意 Windows 应用不是仅安装 SDK 就能自动获得的能力。

## 验收标准

1. 使用用户自己的 API Key 可以创建、继续和恢复 Codex thread。
2. 普通闲聊无需工具即可返回文本，并正确进入现有聊天历史。
3. Agent 能在整台电脑范围内按模式读取和修改用户有权限访问的文件。
4. 三档模式可以在设置页配置，并能在输入框工具栏切换；正在运行的 turn 不受中途切换影响。
5. 建议模式和自动编辑模式的审批请求能显示在现有确认界面并返回批准或拒绝结果。
6. 全自动模式不经过 VoicePet 二次审批，并使用 `full_access` 配置。
7. 文本、命令进度、文件变更、完成、取消和错误能流式显示或转换为现有 UI 状态。
8. Windows 实测取消、Worker 崩溃和应用退出能够清理受管理的 Worker/runtime/命令进程树；错误不会触发重复执行，无法确认的副作用显示 outcome_unknown。
9. API Key、文件正文和敏感命令参数不会出现在日志、审计摘要或诊断包中。
10. 现有聊天、语音、会话和工具测试继续通过；新增 Agent Worker 与模式映射测试覆盖主要生命周期。
11. 前两档在执行前阻止未批准命令；自动编辑的补丁自动通过，但删除、替换和脚本执行等待批准。使用假的执行器和临时测试数据验证，不对用户真实文件做破坏性验收。
12. 模式从全自动切换回建议后，下一轮确实恢复审批；拒绝、重复批准、旧会话批准及取消时收到迟到结果均正确处理。
13. 断线、429、401、无效模型、SDK 缺失和不兼容版本得到明确反馈；工具输出背压不阻塞取消。
14. 打包后的 Windows 程序能用锁定 runtime 完成文字闲聊、临时目录内编辑与无害命令冒烟测试；真实 API 冒烟使用用户配置且不输出 Key。

## 非目标

- 本阶段不实现 ChatGPT OAuth 或 Codex 订阅登录。
- 本阶段不重写 QML 聊天页面和语音管线。
- 本阶段不删除现有工具后端；它作为回退和兼容路径保留。
- 本阶段不承诺绕过 Windows UAC 或执行当前用户无权执行的操作。

## 官方依据

核对日期：2026-09-08。以上产品语义由 VoicePet 定义，不等同于 SDK 原生预设名称。

- [Codex SDK](https://developers.openai.com/codex/sdk/)：Python SDK、异步客户端、固定 runtime 依赖及 sandbox 预设。
- [Codex app-server](https://developers.openai.com/codex/app-server/)：thread/turn 生命周期、审批请求、流式事件、API-key 认证及配置覆盖。
