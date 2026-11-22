# Codex Project Orchestrator Toolkit

项目主控、独立执行聊天、子代理分工、进度持久化和检查点交接的本地工具包。

**0.3.0 包含真正的 MCP 对话工具，不再只有 Skill。** 通过本机 Codex App Server 创建持久的独立聊天，读取完成结果，发送后续任务，并验证后继主控接管。

## 组成

- `skills/project-orchestrator/`：中文协作 Skill 与检查点约定。
- `server/orchestrator.py`：stdio MCP 入口；`rpc_client.py` 管理协议与进程，`workflow.py` 管理状态和调度。运行仅依赖 Python 标准库。
- `scripts/install.py`：通过 Codex 官方 CLI 注册 MCP，并安装/更新同名 Skill。
- `plugin.json`、`mcp.json`：Agent Plugins 1.0 插件描述；`.codex-plugin/` 为兼容描述。
- `tests/`：协议假进程、重复请求、审批边界、交接一致性、崩溃恢复和 MCP 协议测试。
- `THIRD_PARTY_NOTICES.md`、`licenses/`：两个 MIT 上游的固定版本、改动说明和完整许可。

## 运行条件

- Linux / POSIX、Python 3.11+、已登录的 Codex。
- Codex 需要支持 App Server 的 `project/list`、`thread/start`、`thread/read`、`thread/turns/list`、`thread/resume`、`thread/name/set` 和 `turn/start`。
- 本地验收版本：Codex Desktop CLI `0.169.0-alpha.11.1`。其他版本先运行健康检查；实验接口可能变化。
- 安装在实际执行项目工作的主机及用户下。它不会给别的主机、云端 durable 会话或网页会话自动增加本机工具。

## 安装工具与 Skill

将仓库克隆到希望长期保留的位置；安装后不要移动或删除目录：

```bash
git clone https://github.com/Demon0524/codex-project-orchestrator.git
cd codex-project-orchestrator
python3 scripts/install.py
```

已装有旧版同名 Skill 时，审阅后使用：

```bash
python3 scripts/install.py --update
```

可用 `--codex /absolute/path/to/codex` 指定目标客户端的 CLI。安装器只注册 `project-orchestrator` 这一 MCP 服务、复制同名 Skill；遇到已有不同 MCP 配置会停止，避免覆盖。

安装完成后开启新对话，检查是否发现 `orchestrator_health` 并实际调用。MCP 注册成功不代表正在运行的所有聊天会立即刷新工具。如果工具仍未出现，检查该对话所在主机、CODEX_HOME 和 MCP 加载错误。

支持本地插件包的客户端也可按其插件安装流程加载本目录或发行 ZIP。直接 MCP 安装与插件安装二选一，避免同一服务重复注册。普通独立 Skill 安装不会启动 MCP 服务。

## 工具

| 名称 | 功能 |
|---|---|
| `orchestrator_health` | 检查后端连接、项目发现、生命周期和已实现边界 |
| `orchestrator_projects` | 返回本机真实项目 ID、根目录 |
| `orchestrator_create` | 实际新建聊天并启动第一轮任务 |
| `orchestrator_read` | 读取元数据、最近三轮结果及本连接观测到的 token 用量 |
| `orchestrator_wait` | 最多等待 45 秒，返回完成、运行中或需处理状态 |
| `orchestrator_send` | 向工具包创建的聊天发送后续任务 |
| `orchestrator_handoff` | 根据已有检查点创建后继主控，只进行接管核验 |
| `orchestrator_handoff_status` | 仅接受绑定核验轮次的已完成最终回答，并核对检查点与主控记录 |
| `orchestrator_wait_many` | 以同一个截止时间收集多个聊天，支持 any / all |
| `orchestrator_reconcile` | 对账原连接中的迟到创建或续派响应，不自动重发 |
| `orchestrator_resume_create` | 原参数指纹匹配后，显式补完已知创建、尚未发出首轮的聊天 |
| `orchestrator_checkpoint_save` | 验证并脱敏结构化状态，保存不可变检查点 |
| `orchestrator_checkpoint_read` | 校验并读取检查点引用，输出有界 Markdown |
| `orchestrator_handoff_reconcile` | 找回创建阶段的已知后继和核验轮次 |
| `orchestrator_handoff_resume` | 从持久原任务显式恢复创建阶段，不重复创建 |
| `orchestrator_handoff_activate` | 重新检查检查点与主控状态，幂等激活后继 |
| `orchestrator_handoff_activation_reconcile` | 独立对账已发出的激活轮次，不重新激活 |

项目 ID 使用本工具包返回值。桌面连接器的旧项目 ID 不一定与 App Server 的真实 ID 相同，不要混用。

`create` 和 `send` 每次逻辑操作使用唯一、稳定的 `request_id`；重试同一操作沿用原 ID 和参数。请求中途失败时保留 creating / created / sending 状态与已知 ID，不自动重复变更。检查已有聊天和任务结果后再恢复。

## 使用提示词

```text
使用 $project-orchestrator，采用独立聊天协作模式。
先实际调用 orchestrator_health、orchestrator_projects 确认本机后端和项目。
当前聊天担任主控；我授权你在本项目中新建执行聊天、读取结果并发送后续任务。
按需调用 orchestrator_create、orchestrator_wait、orchestrator_read 和 orchestrator_send，记录真实聊天 ID，验收后继续分派。
任务目标：[填写目标]
验收标准：[填写标准]
```

工具名可能带客户端分配的 MCP 前缀；按名称后缀发现，不要假定命名空间。

## 主控交接

1. 主控保存项目进度。可传项目内的 Markdown 检查点文件，也可调用 `orchestrator_checkpoint_save` 保存结构化状态，获得不可变 `checkpoint://...` 引用。
2. 调用 `orchestrator_handoff`，提供源聊天、cwd、目标和 checkpoint / checkpoint_ref 中的一项。源聊天的目录与项目身份必须匹配。原始文件会生成脱敏快照，源文件摘要也保留用于二次检查。
3. 后继独立聊天读取项目指令、不可变检查点和工作区；核验轮次不开展项目工作。
4. 等待后调用 `orchestrator_handoff_status`。只有创建时绑定的核验 turn 已完成，且最终回答以正确接受标记结尾、检查点未变、主控记录无竞争，才记 accepted。
5. 调用 `orchestrator_handoff_activate`：检查点与主控记录再次核对后，激活请求和发送 reservation 在同一事务写入，再开始后继任务。相同交接 ID 不会发送第二次激活。
6. 后继继续在授权范围内工作。旧主控停止派发；后续 `create` / `send` 使用返回的 successor ID 作为 `coordinator_id`。

阶段日志：`prepared → creating → verification_pending → accepted → activation_sending → active`。出现不确定结果时保留 blocked 和已知 ID。

- 创建/续派超时后先用同一个 ID 调用 `orchestrator_reconcile`。晚到响应只在原 Backend 连接中可对账；重启后的 RPC ID 不会误匹配旧连接。
- 已知 thread 已创建而首轮尚未发出，可用完全相同输入调用 `orchestrator_resume_create`；曾发出但结果不明的 turn 不会被重发。
- 交接创建恢复使用 `handoff_reconcile` / `handoff_resume`；激活恢复必须使用 `handoff_activation_reconcile`，两条恢复路径分离。
- 如果连接已经丢失且无法确认结果，保持待处理，不盲目创建或发送。旧版 0.2 交接缺少绑定核验轮次，需要人工重新核验，不会被自动激活。

默认新聊天使用 `read-only`；写任务只有在用户已经授权时才明确选择 `workspace-write`。后继执行也需要写权限时，创建交接时指定该模式，核验轮次仍只做读取。发送后续任务不会提升权限。

主控注册表是本工具包内的交接记录。记录生效后，工具包拒绝旧的或缺失的 `coordinator_id` 发起新调度；该 ID 是调用者提供的协作标识，不是强身份认证，也不是对所有 Codex 对话的全局锁，不会强行终止旧主控。接管标记证明代理声明已核验，不能代替主控对关键产物的验收。

## 生命周期、权限与适用范围

- 如果本机已有受支持的共享 daemon 控制 socket，auto 模式使用 `app-server proxy`；否则运行 MCP 生命周期内的私有 stdio App Server。
- 私有后端在 MCP 断开时退出，运行任务可能中断。已经持久化的聊天可读取，工具包创建的聊天可恢复续派。不要把它描述成无人值守云服务。
- 可用 `CODEX_ORCHESTRATOR_BACKEND=proxy|stdio|auto` 和 `CODEX_ORCHESTRATOR_CODEX` 选择本地后端。工具包不会自动开启 daemon、远程控制或网络监听。
- 审批、秘密、用户输入及未知服务器请求不会自动批准；工具返回需人工处理状态，相关轮次可能失败。它不是完整交互式审批界面。
- 支持读写任务，但不提供权限绕过、全盘授权、自动发布、自动推送或业务部署授权。
- 不连接云端 durable 主机，不修复它的离线状态，也不猜测该主机的公网入口。
- token 数据仅为当前后端连接收到的事件，可能为空；**没有精确阈值自动轮换或永久后台调度器**。自动换主控由具备这些工具的运行中主控在阶段检查点调用。
- 本工具包的状态默认位于用户 `~/.local/state/codex-project-orchestrator`，保存操作指纹、聊天 ID、交接日志、原核验任务和结构化检查点，文件权限为 0600。检查点的常见凭据过滤为尽力检测，不保证识别所有秘密。不要把状态目录上传仓库。

## 验证

```bash
python3 server/orchestrator.py --health
python3 -m unittest discover -s tests -v
# 明确启用后会创建验收聊天并调用模型
python3 scripts/acceptance.py --live
```

已在本地真实后端完成：新建持久聊天 → 读取返回结果 → 续派第二轮 → 创建后继 → 核验检查点 → 确认接管；桌面原生读取工具也能读到新建的同一聊天 ID。独立对话也已发现并实际调用注册后的 MCP `orchestrator_health`，调用完成且只读标记正确。这些验收不代表其他主机、版本或当前云端会话已接入。

## 更新与卸载

更新仓库后执行 `python3 scripts/install.py --update`，然后在新会话验证工具。

```bash
codex mcp remove project-orchestrator
```

该命令只移除 MCP 注册，不删除项目、历史聊天或检查点。同名 Skill 可另行手动移除。

## 上游合并范围

- **session-handoff**：实际复用其结构化状态验证和 Markdown 渲染；在本项目中接入不可变检查点、尽力脱敏与阶段日志。未安装其 CLI launcher、跨客户端迁移或 telemetry。
- **codex-app-server-mcp**：将有界迟到响应跟踪和进程生命周期处理移植到 Python 后端；未捆绑整套 TypeScript 服务，也未引入其 Node.js 依赖。

固定提交、原文件、改动和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 打包

```bash
python3 scripts/package.py
```

打包器只包含发布源码、技能、测试和许可，排除 `.git`、运行状态、验收目录和缓存。更改源码后必须重新打包。
