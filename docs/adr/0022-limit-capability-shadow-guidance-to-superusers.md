# ADR-0022：只向 SUPERUSER 接入能力影子候选检索

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-12 |

## 当时遇到了什么

部署实测已经从 35 个能力所有者生成 624 条影子记录，但其中只有 1 条 `public`，另有 559 条
`review` 和 64 条 `restricted`。例如“搜图”可以命中当前已加载插件，却只有命令头和插件说明，handler、
场景、配置与外部服务条件仍是不透明约束。直接把这些候选交给普通用户，会把“发现到”误写成“已经复核且
可以执行”，也会越过 ADR-0021 的披露边界。

项目已经把 NoneBot `SUPERUSER` 定义为部署开发 / 维护者。维护者需要检查这些候选和受限能力，但第一步
不需要模型生成答案，也不应改变普通用户的公开能力范围。

## 决策

1. 普通用户继续只读取显式公开 Provider；`review` 和 `restricted` 不进入普通用户回复或模型上下文。
2. `triage <能力问题>` 先尝试高置信的显式公开能力。没有明确命中时，只有通过当前 Bot 和 Event 的
   NoneBot `SUPERUSER` 检查，才允许读取影子索引的 `public + review + restricted`。
3. `CapabilityShadowService` 持有索引路径和披露开关，向已经完成鉴权的调用方提供窄维护者检索入口；
   handler 不直接设置 `include_restricted=True`。
4. 回复使用确定性模板并标明 `已登记公开能力 / 未审核候选 / 维护者可见受限能力`。没有可靠 `usage` 时
   明确要求核对当前源码、README 或插件自带帮助；存在 `opaque` Constraint 时明确说明静态无法判断。
5. 发现、可见和当前可执行严格分离。即使是 `SUPERUSER`，最终执行资格仍由原插件的 Permission、Rule、
   handler、配置、场景、限流和外部状态决定。
6. 索引未配置、未就绪、schema 不兼容或查询失败时回退现有显式 Provider，不阻断求助入口；partial 快照
   向维护者显示覆盖不完整提示。启动刷新失败但上一份成功构建的索引仍可读时可继续检索，但必须标明 stale，不能
   把旧 generation 冒充当前部署事实。`partial` 随索引 metadata 持久化；旧格式没有该字段时必须提示完整性
   未知，不能默认当成完整。
7. 影子字段仍是不可信文本。进入群消息前必须折叠换行、限制长度、移除 Unicode 控制字符并中和 mention；
   不能让插件元数据或文档说明触发群提醒或制造双向文本混淆。
8. 本切片不启用模型、不执行命令，也不实现普通用户的 `review` 审批。未来公开候选必须使用部署本地审批
   策略，并绑定被复核的能力选择器与来源修订；源码或版本变化后应回落 `review`。

## 为什么这样选

- SUPERUSER 鉴权发生在索引和模型之前，受限能力不会先泄露再依赖提示词保密；
- 维护者可以立即审计本机候选覆盖，普通用户的现有行为和披露范围不变；
- 确定性模板能验证检索、披露标签和失败回退，不需要提前放行模型；
- 候选说明不冒充执行授权，为后续字段级审批和本地 RAG 留出边界。

## 没有采用的方案

- **普通用户直接读取全部 review**：覆盖率高，但没有完成披露复核，且可能暴露内部能力。
- **让模型判断是否应该隐藏能力**：鉴权发生得太晚，模型已经看到受限数据。
- **SUPERUSER 看到候选就视为可以执行**：影子采集不会运行第三方权限、规则或 handler，无法作出这种保证。
- **先接生成式 RAG**：当前精确 FTS 与固定文案已经足以验证第一条在线检索链路。

## 带来的影响

- 同一个 `triage` 入口对普通用户保持原行为，对 SUPERUSER 增加候选审计回复；
- `restricted` 的本地保护责任不变，operator exclude policy 仍负责未来的源头完全排除；
- 当前回复只能复述索引中已有字段并揭示缺口，不能生成未被证据支持的参数或示例。
- 上一次成功构建的索引可以作为维护者的陈旧参考，但必须显示 stale；所有回显先经过纯文本安全处理。

## 落实与确认

- `CapabilityShadowService.search_for_maintainer` 封装全部披露层检索；handler 在显式 Provider 未命中后执行
  NoneBot `SUPERUSER` 检查；格式化器对 disclosure、partial、缺失 usage 和 opaque constraint 给出固定说明。
- 自动测试覆盖普通用户无法读取 review、SUPERUSER 可读取候选、三种披露层均进入维护者搜索、索引未就绪
  时失败关闭，以及无可靠 usage 时不编造参数。
- nonemigut 部署实测生成 624 条记录并精确命中 YetAnotherPicSearch 的“搜图”候选；SQLite 完整性检查通过，
  快照 `partial=False`。这只验证检索链，不把该候选自动批准为普通用户 public。

## 替代关系

- 部分替代 [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) 中“第一阶段不接入群聊回复”和
  “群聊 SUPERUSER 尚未接入”的实施边界；影子采集、披露、证据和执行资格边界继续有效。
- 补充 [ADR-0020](0020-use-triage-command-for-natural-language-support.md)：不增加新命令，继续使用必选
  `triage` 与可选 `@Bot` / Reply。

## 相关文档

- [部署本地能力影子索引](../architecture/flows/capability-shadow-index.md)
- [triage 自然语言支持入口](../architecture/flows/support-intake-routing.md)
