# ADR-0006：首个报障入口采用 Alconna 跨平台外壳与可插拔引用 Provider

| 状态 | 决策日期 | 替代关系 |
|---|---|---|
| 已采纳 | 2026-08-09 | 替代 ADR-0005，并替代 ADR-0004 的 OneBot 专属入站引用与解析部分 |

## 背景

ADR-0005 曾把首个用户入口冻结为 OneBot V11 `on_fullmatch`、`GroupMessageEvent` 与 `GROUP` 权限。
该方案可以快速 dogfood，却会让命令、回复提取、场景判断和消息发送四个公开边界都依赖单一协议。项目尚未
发布，也没有兼容负担，应该在第一条真实入口形成前纠正，而不是后续再迁移。

锁定版本 `nonebot-plugin-alconna 0.62.1` 已提供所需的公共跨平台语义：`on_alconna` 注册 Matcher，
`OriginalUniMsg` 把适配器回复转换为统一 `Reply` 段，`MsgTarget` 描述群、频道和私聊目标，
`UniMessage` 负责按当前适配器发送结果。运行观察核心本来就是传输无关的，因此不需要把 OneBot 类型继续
带进报障服务。

但 UniSeg 不能自动解释每个适配器平台 API 的发送返回值。用户回复 Bot 主动输出时，要把该输出的消息
引用关联回原事件 correlation ID，仍需要适配器级的出站 Provider。

## 决策

1. 用户可见入口从第一版起采用 `on_alconna(Alconna(command))`、`to_me()` 与 `use_cmd_start=False`；
   命令文本仍可配置，当前保持精确“报错”，不引入自由文本意图判断；
2. handler 只依赖 NoneBot `Bot` / `Event` 和 Alconna 的 `OriginalUniMsg` / `MsgTarget`。它只读取第一个
   统一 `Reply.id`、`Event.get_user_id()`、Bot self ID 与目标路由字段，不读取被回复正文、Reply origin
   或适配器事件私有字段；
3. 公共回复用 `UniMessage` 发送。插件元数据声明 Alconna UniSeg 当前支持的适配器模块，不再声明仅
   OneBot V11；未安装 OneBot 依赖时插件仍可加载；
4. 新增 `UniversalReferenceBridge`：入站消息通过 UniSeg `get_target` / `get_message_id` 统一绑定。
   稳定会话 scope 排除事件 source，原始适配器、Bot、会话与消息标识仍只瞬时进入 HMAC 索引；
5. 出站引用采用可插拔 Provider。OneBot V11 是首个 Provider，只负责从成功群发送 API 的结构化结果提取
   `message_id`；后续 QQ 官方或其他适配器分别实现并做集成测试，不修改领域服务；
6. 跨平台入口能力与出站关联覆盖必须分开描述：所有 UniSeg 支持且能产生 Reply / Target 的适配器可提交
   入站消息报障；“回复 Bot 输出也能关联”只对已经实现出站 Provider 的适配器成立；
7. ADR-0005 的窄回显、普通成员可提交、HMAC 限流、短期内存、无模型 / GitHub / Probe / 修复副作用等
   其余安全约束继续有效。当前只支持非私聊目标，私聊明确返回不支持。

## 选择理由

- 命令、回复、目标和发送同时跨平台，领域服务不再随适配器数量分叉；
- `OriginalUniMsg` 附加的是适配器已经解析出的结构化 Reply，避免读取正文后猜测引用；
- 入站统一、出站分 Provider 如实反映各平台 API 差异，避免“用了 Alconna 就天然全平台关联”的过度承诺；
- OneBot 仍是首个 dogfood 和出站 Provider，不浪费既有实测，同时不会成为核心依赖；
- 原始身份只在当前调用栈参与 HMAC，现有隐私、TTL、容量和跨 scope 不变量保持不变。

## 代价与限制

- 需要维护 Alconna / UniSeg 的锁定版本兼容测试；适配器升级可能改变 Target 或 Reply 映射；
- 当前只有 OneBot V11 出站 Provider，其他平台回复 Bot 输出时可能得到“近期运行记录不可用”；
- 插件元数据表示入口可适配范围，不代表每个适配器都完成真实平台端到端测试；支持矩阵必须另行维护；
- `Event.get_user_id()`、Target 与 Reply 不可用时入口失败关闭为公开错误，不回退到正文解析或时间猜测；
- 当前仍只支持群聊 / 频道显式报障，不把跨平台等同于开放私聊或任意历史查询。

## 参考

- [Alconna Matcher](https://nonebot.dev/docs/best-practice/alconna/matcher)
- [UniMessage 跨平台消息](https://nonebot.dev/docs/best-practice/alconna/uniseg/message)
- [UniSeg 消息段](https://nonebot.dev/docs/best-practice/alconna/uniseg/segment)
- [ADR-0004：OneBot V11 与带密钥引用索引](0004-onebot-v11-first-and-keyed-message-reference-index.md)
- [ADR-0005：原 OneBot 群报障交互策略](0005-first-group-report-interaction-policy.md)
