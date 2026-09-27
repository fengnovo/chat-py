---
name: "failed-run-continuation"
description: "在 Agent/聊天系统中为失败（终态）的任务实现「一键续跑」：新建 run、服务端合成内部续跑指令、不留下任何用户「继续」消息记录。当需要实现/排查失败任务续跑、恢复中断的 agent run、或「继续」按钮只关闭横幅不发请求等问题时调用。"
---

# 失败任务续跑模式（Failed Run Continuation）

把「任务失败后点按钮，等价于替用户发一次『继续』，但界面和历史里都不出现用户说的『继续』」沉淀为标准实现。适用于 LangGraph/LangChain agent + SSE 流式聊天 + AI SDK `useChat` 类技术栈，其他栈可照搬分层思路。

## 一、先判定：是「终态失败」还是「连接中断」

两种情况处理完全不同，不要混用：

- **run 终态失败（failed）**：graph 执行已异常退出，流正常结束。只能**新建 run**，不存在 resume。特征：收到了 `run.failed` 终态事件、数据库 run `status='failed'`。
- **连接中断（stream error）**：任务可能还在后台跑。应该 **resume 同一条流**（重连 SSE / 按 cursor 补帧），不能新建 run，否则重复创建任务。

前端通常对应两种 UI：失败横幅（本技能）+ 连接错误横幅（重新连接/重新提交）。

## 二、四个必须接受的机制约束

1. failed 是终态 → **只能新建 run**。
2. checkpoint 按会话绑定（本项目 `thread_id = sessionId`）→ 同会话的新 run **天然能看到全部历史消息、工具调用与结果**，续跑不需要重放上下文。
3. 框架要求新 run 必须带一条**新的 human 输入**才能重新唤醒模型，「什么都不发」做不到。
4. 这条 human 输入不能是用户真的说的话 → 由**服务端合成内部指令**，并在所有用户可见面（前端气泡、历史接口）隐藏。

## 三、四层实现

### 1. 数据库：给 run 打续跑标记

- 迁移给 runs 表加 `continuation boolean NOT NULL DEFAULT false`（默认 false 保证旧数据/普通 run 无影响）。
- schema、record 映射、`createRun` 入参全部透传该字段。
- 新增「按外部会话键**只查不建**」的查询方法（如 `getSessionByExternalKey`）：续跑必须依附既有会话，会话不存在返回 404，绝不隐式创建或重命名会话。

### 2. API：服务端合成指令，客户端内容不可信

聊天接口（如 `/api/chat`）增加 `continuation: boolean`：

- `continuation=true` 时：
  - 必须带会话标识，否则 400；只查既有会话，不存在 404；
  - **完全忽略客户端传来的消息内容**，统一使用服务端常量指令（防伪造、防脏数据）；
  - 不带附件、不更新会话标题；
  - `createRun({ continuation: true })`。
- **历史接口过滤**：拼装会话消息时，`run.continuation` 的 user 气泡直接跳过；若续跑产出了正文，仍正常返回 assistant 消息。

参考指令文案（关键是指向「上一条用户消息」而非「原始任务」，避免长会话歧义）：

```
上一轮任务因执行错误中断了。请基于上方对话和已完成的工作，继续完成上一条用户消息所要求的任务；先检查当前进度，不要重复已经完成的步骤。
```

文案设计原则：① 唤醒模型；② 明确目标是**上一条用户消息**（长对话/多任务不歧义）；③ 要求先查现状、别重复（保护写文件/发消息/部署等有副作用的操作）。

### 3. 前端：隐藏占位消息驱动请求，结束即清理

以 AI SDK `useChat` 为例（核心是「需要一条 user 消息驱动请求，但全程不可见」）：

1. 按钮从「仅 dismiss 横幅」改为调用续跑函数。
2. 续跑函数生成**固定 `messageId`**（UUID），加入 `hiddenContinuationIds` 集合，再
   `sendMessage({ text: '继续', messageId }, { body: { continuation: true } })`。
   - 固定 id 是关键：消息在 store 中一出现就能在渲染层按 id 过滤掉，不会闪现。
   - `body.continuation` 通过 transport 的 `prepareSendMessagesRequest` 透传（从 options.body 取出并入请求体）。
3. 渲染 `messages.map` 时，命中 hidden 集合的消息返回 `null`。
4. 清理 effect：当 `status` 落回 `ready/error` 且 hidden 集合非空时，从 store 删除占位消息并清空集合。
5. 切换会话 / 新建会话（reset）时同步清空 hidden 集合。
6. 横幅策略：**发起即消失**（点按钮立即 `setRunFailure(null)`）；新 run 再次收到 `run.failed` 事件时横幅自动重现，可反复续跑。续跑函数要防重入（busy/error 时直接 return）。

### 4. 验证清单

- 续跑中界面无「继续」用户气泡；刷新页面、翻历史也没有（历史接口已过滤）。
- 新 run 在 DB 中 `continuation=true`，`user_message` 为内部指令。
- 续跑的模型请求能看到之前全部上下文，且不重复已完成步骤。
- 再次失败：横幅重现；成功：横幅不再出现（页面挂载拉 latestRun 失败态的逻辑保持不变）。
- 对不存在的会话发 continuation 返回 404，不会新建空会话。
- 类型检查 + 仓储层形状测试（INSERT 列顺序变化后同步 mock 索引）。

## 四、为什么不选「重发用户原话」

| 维度 | 内部续跑指令（采用） | 重发上一条原话 |
|---|---|---|
| 短对话 | 正常续跑 | 可能从头重复 |
| 长对话/多任务 | 指向「上一条消息」即不歧义 | 更像重新提问，重复概率高 |
| 副作用操作 | 明确先查现状、别重复，较安全 | 有重复执行风险 |
| 附件 | 纯文本无负担 | 需重新挂载附件 |

## 五、本项目实现锚点

- 迁移：`packages/db/migrations/011_run_continuation.sql`
- 仓储：`packages/db/src/repository.ts`（`continuation` 字段、`getSessionByExternalKey`、`createRun`）
- API：`apps/api/src/routes.ts`（`CONTINUATION_INSTRUCTION`、`/api/chat` 续跑分支、history 接口过滤）
- 前端：`apps/web/app/components/resilient-chat/chat-runtime.tsx`（`continueAfterFailure`、`hiddenContinuationIds`、清理 effect）、`failure-notice.tsx`（`onContinue`）

## 六、易踩的坑

- 占位消息不设固定 `messageId` → 首帧渲染时用户气泡会闪现。
- 只在前端隐藏、后端历史接口不过滤 → 刷新/换设备后「继续」消息冒出来。
- 用客户端传来的文本作为续跑指令 → 可被伪造注入任意指令，必须服务端合成。
- 把 failed 当连接错误去 resume → 终态流无法恢复，静默无效。
- INSERT 语句加列后参数占位符序号整体后移，仓储形状测试的 mock 索引必须同步改。
