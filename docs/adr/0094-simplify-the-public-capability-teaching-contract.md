# ADR-0094：收敛公开能力教学合同并统一有限枚举

| 状态 | 决策日期 |
|---|---|
| 已采纳；family 边界被 [ADR-0095](history/0095-preserve-family-member-invocations-and-compress-only-display.md) 替代；真实插件诊断范围被 [ADR-0097](0097-capture-complete-capability-model-output-in-explicit-maintenance-runs.md) 部分替代；Migut Help description 边界被 [ADR-0100](history/0100-keep-migut-help-descriptions-minimal.md) 替代；路由、授权与业务准备状态的字段所有权被 [ADR-0113](0113-separate-routing-authorization-and-business-readiness-in-teaching.md) 部分替代 | 2026-08-19 |

## 当时遇到了什么

教学注释先后为 Help、Answer、Bug 预检、旧内容增量改写和门禁闭合增加了多组字段。真实 Provider
评测显示，`synonym / supported_subject`、`input_requirement / input constraint`、
`behavior_boundary / rate_limit` 之间没有唯一的事实所有权；模型会把同一事实重复写入不同字段，也会把
更具体但仍兼容的当前说明误判为对旧成员的删除或替换。`feature_state` 与 `other` 又成为难以验证的逃生口。

同时，自由 `answer_markdown` 形成了第二套公开事实通道：它可能重复结构化字段，也可能覆盖确定性 fallback，
但现有 family held-out 没有证明它承载了结构化合同无法表达的必要事实。Migut Help 与 Answer 的展示形式不同，
不应反过来要求核心教学层保存两份语义。

参数化 Matcher family 还暴露了另一类边界。family 是与普通 Matcher 同级的 teaching unit，保存共同公开语义；
具体成员仍来自当前 Runtime Matcher 索引。为了避免为 meme、下载器或其他单个插件设计 `members / variants /
catalog` 等专属字段，需要在通用查询层解决“小集合明确列出、大集合聚合表达”，而不是另建成员目录。

## 决定

### 公开条目只保留六类字段

1. schema 7 的每个公开教学 entry 只保存：
   - `name`：简短能力名称；
   - `summary`：一句话用途；
   - `usages`：完整、可直接展示的调用形式；
   - `search_terms`：只用于召回的名称、同义表达和处理对象词；
   - `behavior_boundaries`：usage 无法表达、但用户正确使用或理解结果时需要知道的输入格式、后续交互、
     处理范围、结果范围和业务边界；
   - `requirements`：影响能力能否执行的公开前提，只允许 `role / scene / access / rate_limit`。
2. 删除模型与公开缓存中的 `synonyms`、`supported_subjects`、`input_requirements`、`answer_markdown`，并删除
   requirement kind `input / feature_state / other`。旧 schema 缓存完全失效，不读取、不迁移，也不把旧字段
   猜测映射成新活动合同。
3. 同一事实只有一个语义所有者：
   - parser 或 canonical usage 已完整表达的命令头、参数必选性、可选性和重复性，只进入 `usages`；
   - 输入编码、回复上下文、后续交互及能力自身的结果或业务范围进入 `behavior_boundaries`，不得重复 usage；
   - 角色、会话场景、授权范围、冷却、配额和并发只进入 `requirements`，不得再复制为行为边界；
   - 不能可靠归入上述字段的 gate 不写成 `other`。补证后仍无法确定时保持 `unresolved`，并关闭该 teaching
     unit 的公开知识。

### 生成内部表示与公开合同分层

4. Claim 继续作为生成和 Evidence 引用闭包的内部事实表示，Constraint 继续表示影响执行资格的内部候选；
   两者不作为最终公开字段名。模型外固定的角色、场景等安全下限无条件合并，模型只能增加证据支持的限制，
   不能删除或放宽。
5. `gate_resolutions` 只用于证明每个 gate candidate 已被解释为 `constraint / no_constraint / unresolved`；
   `baseline_changes` 只用于对上一版非证据编辑基线提交 `keep / replace / remove`。两者都是生成协议，不是
   Help、Answer 或 Bug 消费者可见的公开知识。上一版 `requirements` 不作为生成文字回送模型；角色、场景、
   访问资格和限流每轮只按当前 gate、Runtime 与源码 Evidence 重建，避免未经本轮验证的授权措辞被直接沿用。
6. `access` 只保存脱敏后的可观察效果，例如“需授权”或“可能只对部分用户、群或场景开放”。不得公开黑白名单
   成员、ID、配置键，也不得仅凭该字段断言当前用户或群一定命中名单。纯黑名单仍可生成保守的“使用资格
   可能受限”，以便后续流程知道失败不一定是命令写错。
7. 任意普通文本中的 `@用户` 不再被公共文字校验器一概拒绝。只有 usage 中的精确占位符 `@bot` 具有
   “需要提及机器人”的结构语义；NoneBot / QQ 的真实 mention 仍由结构化消息段负责，裸文本不会被误当成
   已经构造了 At 消息段。

### Help、Answer 与 family 不再维护第二套事实

8. Migut Help 是有损展示适配器：显示 name、summary、主 usage、可投影角色和冷却，并把 `access` 统一显示为
   “需授权”。核心合同仍假设所有公开字段都可能被 Answer 或后续消费者暴露，不能把敏感信息藏在“Help
   默认不显示”的字段里。
9. Answer knowledge Markdown 继续作为发布文件格式，但正文必须由结构化 entry 确定性渲染；模型不再生成
   自由 Markdown。Answer 检索使用 `search_terms`，回答使用 usages、behavior boundaries 和 requirements。
10. family 仍是与普通 Matcher 同等级、不可拆分的 teaching unit，并只保存共同 name、summary、usages、
    search terms、behavior boundaries 与 requirements，不持久化成员目录。查询命中 family 时，从本轮已注册、
    公开且与该注释精确绑定的 Runtime Matcher 记录补充当前具体命令；这不是新的 LLM 工具，也不引入另一份
    cache、数据库或插件专属协议。

### 四个是所有固定备选的通用展示边界

11. 同一位置的确定固定备选不超过四个时，公开 usage 使用括号逐项列出；超过四个时使用一个简短概念槽位，
    不把完整成员表塞进注释。该规则同时适用于：
    - parameterized family 的 Runtime Matcher 成员；
    - 单个 Matcher 的多个固定命令头、alias、Option 或固定参数值；
    - 其他能够确定性证明完整集合的 usage 位置。
12. 小 family 查询可以从当前 Runtime 索引有界补充二至四个精确成员；大于四个时只保留聚合 usage 和当前
    命中的具体 Matcher，不枚举全部成员。若共同语义、输入形状或 requirements 无法可靠成立，family 关闭
    知识，不能用成员名称掩盖异构行为。

### 评测与诊断

13. schema 7、Prompt v39 与 request v3 是新的精确评测身份。已经消费的 v12/schema 6/request v2 保留为
    历史报告，不能继承资格，也不能修改后重新冒充 forward-heldout；正式质量结论必须使用全新冻结 fixture。
14. 默认运行时遥测继续遵循 ADR-0089，不保存 Prompt、源码或模型正文。仅维护者显式开启、且运行完整精确
    官方合成 fixture 时，评测器可把失败或经过 correction 的 assistant 输出、tool call/result 和稳定错误码
   写入本地 ignored 诊断文件。该文件不得包含 API key、初始 Prompt 或真实插件私有源码，也不进入仓库。

### Alconna shortcut 是调用 Evidence，不是 alias

15. 标准 Parser usage 与 shortcut 分开保存生成职责：
    - `canonical_usages` 继续锁定标准命令头、参数顺序、必选性、Option 和重复性，模型必须保留；
    - 当前 Runtime 已注册的 shortcut 以有界 Evidence 提供其原始 pattern、显式 `humanized`、目标命令、固定
      参数、prefix/fuzzy 标志和本地 wrapper 符号；不得把它压成只替换命令头的 alias；
    - 模型可以结合 pattern、改写参数和源码理解生成额外的可读 usage，但必须引用 shortcut Evidence，不得
      执行 wrapper、公开原始正则或内部符号；Evidence 不足时只省略 shortcut，不影响标准 usage 与知识开放；
    - 标准 usage 与 shortcut usage 共用公开条目的三条显式 usage 上限。最终公开 Schema 不增加 `shortcuts`
      字段，所有可展示形式仍归入普通 `usages`。

## 为什么这样选

- `search_terms` 对应唯一的检索职责，避免模型区分两个消费者完全相同的文字列表；
- `behavior_boundaries` 承接真正的用户说明，但不成为新的 `other`，因为权限与限流仍有唯一结构所有者；
- 四种 requirement 都有明确消费者或安全意义：角色、场景、授权范围和限流可以被展示、回答或预检使用；
- 删除自由 Markdown 后，发布文件仍保持适合 Answer 的可读格式，但不再允许它覆盖结构化真值；
- Runtime 已经拥有当前 Matcher 成员身份。查询时有界组合它与 family 共同注释，比持久化第二套成员目录更
  新鲜，也不会为某个插件发明专属 schema；
- 固定四个作为通用展示政策，比为 family、alias、Option 和固定参数分别维护阈值更容易解释和验证。

## 没有采用的方案

### 合并 Claim 与 Requirement

Claim 是生成阶段的 Evidence 归属与增量编辑载体，Requirement 还承担模型外固定安全下限、Help 结构投影和
执行资格语义。把两者合并会让普通功能说明与“满足什么条件才能执行”再次混在一起，因此只简化公开字段，
不删除内部职责分层。

### 保留 `other` 作为兼容逃生口

历史正式 held-out 没有要求 `other`；本地唯一输出只是把可选 `--quiet` 错写成前提。保留它会让不确定 gate
伪装成已理解 requirement。已证实但无法映射到四类的事实可以作为行为边界时进入该字段；若它确实决定能否
执行却仍无法分类，则保守关闭，等真实案例证明需要新的通用 requirement kind 再扩展。

### 为某个插件新增成员目录或模板字段

不新增 `members / variants / catalog / image_count / text_count / template_options`，也不引入 MemberCatalog、
SQLite、向量索引或动态目录协议。具体 Matcher 身份由通用 Runtime 索引提供，共同公开知识由普通 family
entry 提供。

### 保存所有 Provider 消息以便排错

不改变生产隐私边界，也不保存初始 Prompt 或真实源码。只在精确官方合成评测、维护者显式 opt-in 时保存必要
的无效输出和 correction 轨迹；范围扩大必须另行决策。

## 带来的影响

- 有利：模型不再需要在语义重叠字段间猜分类，baseline 合并和 Oracle 都可以围绕更少的唯一事实所有者；
- 有利：Help、Answer 与 Bug 预检消费同一公开合同，自由 Markdown 不会成为旁路；
- 有利：白名单或其他访问范围可以安全表达“需授权”，又不泄露成员或武断判断当前主体；
- 有利：family 和普通 Matcher 的固定备选共享同一四项展示规则；
- 有利：Alconna shortcut 可以进入教学知识，同时不放宽 Parser 对标准用法的结构安全下限；
- 代价：真实但无法映射到四类的执行 gate 会失败关闭，直到有跨插件案例证明需要新增通用类别；
- 代价：schema 6 缓存全部重生成，历史 Provider 质量结论不能继承；
- 代价：大 family 的 Answer 只拥有聚合知识与当前召回的具体成员，不把全部成员细节永久塞入模型上下文。

## 落实与确认

- `capability_analysis.py`、`capability_annotations.py` 与 `capability_model_adapter.py` 实现新内部和公开合同；
- `capability_snapshot.py` 只读取已注册 shortcut 的有界结构事实，不执行 wrapper；request v29 将这些事实绑定
  到普通 Matcher invocation，并使旧 baseline requirements 不再进入模型 payload；
- `capability_usage.py` 统一实现四项有限枚举及概念槽位校验；
- `capability_shadow.py` 从当前 Runtime 索引有界组合小 family 成员，不保存新成员目录；
- `capability_help_display.py` 与 `capability_teaching_outputs.py` 分别做 Help 有损投影和 Answer Markdown
  确定性渲染；
- maintainer evaluator 只在显式合成评测边界保存无效输出诊断，并拒绝旧 v12 取得新合同资格；
- 领域、adapter、发布、Help、Answer、Bug 公开合同与 evaluator 测试共同覆盖上述边界。

## 替代关系

- 替代 [ADR-0062](history/0062-structure-capability-teaching-usages-requirements-and-interactions.md) 的公开字段和
  requirement kind；其 Runtime/模型事实所有权、结构化限流和不按符号名猜门禁的原则继续有效。
- 替代 [ADR-0069](history/0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md) 中模型生成
  Answer Markdown 的决定；Help 与 Answer 仍是两个展示适配器，并由同一 generation 原子切换。
- 接续 [ADR-0080](0080-model-capability-teaching-as-multiple-public-entries.md) 的多 entry 身份，不改变
  Alconna 叶子与普通 teaching entry 的边界。
- 细化 [ADR-0081](history/0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md) 的四项有限枚举：阈值
  适用于所有固定备选，不只参数化 family。
- 延续 [ADR-0082](history/0082-group-parameterized-matchers-only-by-runtime-handler-code-identity.md) 的 family 身份，
  只改变查询时如何组合当前 Runtime 成员与共同注释。
- 对 [ADR-0089](0089-persist-redacted-pydantic-ai-agent-traces.md) 增加仅限官方合成评测 opt-in 的诊断例外；
  生产默认遥测仍不保存正文。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
- [模型与 Provider 支持矩阵](../architecture/model-provider-support.md)
