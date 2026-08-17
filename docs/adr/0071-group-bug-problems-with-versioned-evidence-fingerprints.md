# ADR-0071：用版本化 Evidence 指纹聚合 Bug Problem

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；Bug 的版本化 Evidence 签名与保守聚合已实现，unknown 回执由 ADR-0078 暂缓 | 2026-08-15 |

## 背景

[ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md) 已经分离 Report、Occurrence 与 Problem，
但尚未决定两个 occurrence 何时属于同一个 Problem。当前 `BugCaseFingerprint` 同时包含用户请求摘要、
failure signature、subject、adapter、源码 revision、教学合同 revision 与部署 generation；runtime 又暂时把
请求文字、Thread 上下文和 Reply 正文的摘要当作 failure signature。这样会让同一个 Bug 因用户换一种说法而
重复建档，也会把“案件输入身份”“问题根因身份”和“旧 verdict 的版本适用性”混在一起。

问题标题和摘要是给人阅读的自然语言，可以由 LLM 生成和修改，但不能成为聚合身份。LLM 可以基于跨文件源码、
运行、日志、设计与部署 Evidence 提出原因候选；它不能仅凭两段描述相似就选择已有 `problem_id`。自动聚合必须
建立在模型外能够从引用 Evidence 重新计算的技术身份上。

本决定参考两类成熟实践：

- Sentry 使用 Event fingerprint 把具体 Event 归入 Issue，并主要依赖 stack trace 等技术信息；标题、状态和
  生命周期与 grouping identity 分离；
- GitHub Code Scanning / SARIF 使用版本化 `partialFingerprints` 跨分析运行识别同一结果；显示用的
  `message.text` 与身份字段分离，并避免把绝对行号等不稳定位置放进指纹。

参考：

- [Sentry：Enriching Events 与 Event Fingerprinting](https://docs.sentry.io/platforms/javascript/guides/tanstackstart-react/enriching-events)
- [Sentry：Issue Details](https://docs.sentry.io/product/issues/issue-details/)
- [GitHub：SARIF support for code scanning](https://docs.github.com/en/enterprise-cloud@latest/code-security/reference/code-scanning/sarif-files/sarif-support)
- [OASIS：SARIF 2.1.0 partialFingerprints](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)

## 决定

### 展示与身份分离

1. `display_title` 和维护者摘要是可修改的自然语言展示字段，不参与 Problem identity。修改标题不得改变
   `problem_id`，相似标题也不得自动合并问题。
2. Bug Agent 只可以输出受限 `cause_kind`、Evidence ID、责任候选和展示文字；不能直接指定、接受或修改
   `problem_id`。模型可以返回“可能相关”的维护者提示，但该提示不产生聚合副作用。

### 版本化部分指纹

3. 模型外 `ProblemSignatureBuilder` 从 reconciler 已接受的 Evidence 中生成版本化、结构化
   `ProblemSignature`。签名由 `signature_kind + algorithm_revision + required_parts` 组成；只有当前 kind 的必需
   部分全部可验证时才称为完整签名。
4. 第一版只支持能够覆盖当前常见 Bug 的少量签名种类：
   - `exception_path`：subject、异常类型和稳定符号 / 责任边界；
   - `contract_outcome`：subject、被违反的 active contract fact、规范化实际 outcome 和可验证执行阶段；
   - `implementation_invariant`：subject / component、固定不变量种类和稳定符号 selector；
   - `api_failure`：subject、adapter / API action、稳定错误类别或错误码和责任边界。
   实施时可以合并语义完全相同的 kind，但不得增加任意自由文本签名。
5. Problem signature 不包含用户请求或 Reply 文字、LLM 标题 / 摘要、绝对路径、绝对行号、完整 traceback、
   source revision、contract revision 或 deployment generation。源码、合同和部署 revision 继续属于 Occurrence
   与 DecisionApplicability，用于决定旧 verdict 能否复用，不因普通版本变化自动切断长期 Problem 身份。
6. 指纹算法名和版本必须持久化。算法升级时只使用两条 occurrence 都具备的最新共同版本比较；不得用新算法
   静默重写历史 Problem identity。需要迁移时保留旧签名和显式迁移记录。

### 自动聚合与失败闭合

7. 两个 occurrence 只有在同一受支持 `signature_kind` 的完整指纹匹配、subject 与必要平台适用范围兼容，并且
   当前 Evidence / revision 没有冲突时，才能自动归入同一个 Problem。这里的“完整”只指该 kind 的少量必需
   字段，不要求所有聊天、版本、源码和部署字段同时存在。
8. 用户换一种说法、不同用户分别报告、源码只发生无关编辑或 Problem 标题改变，均不得单独造成新 Problem；
   只要当前 occurrence 重新调查后得到同一完整技术签名，就应自动关联。已解决 Problem 再次匹配时记录新的
   Occurrence，并把生命周期标成回归，而不是创建新编号。
9. 相同用户文字、相同命令或相同插件不构成自动聚合依据。技术签名不同必须建立不同 Problem；无法构造完整
   技术签名时建立 `grouping unresolved` 的独立 Problem，最多向维护者提示可能相关，不能自动合并。
10. 错误拆分通过维护者 merge 与签名 alias 可以恢复；错误合并必须支持 split 并重新计算派生计数。merge / split
    的命令形状和事务实现在后续生命周期决定中收敛，本决定先保留领域能力要求。

### 可验收的体验边界

11. 聚合实现和评测必须至少证明：
    - 同一异常 / 根因的两种用户说法自动归入一个 Problem；
    - 不同用户独立复现同一根因形成多个 Occurrence、一个 Problem；
    - 同一入站重试或同一 correlation 不重复增加 Occurrence；
    - 源码发生无关编辑后，同一技术签名仍归入原 Problem；
    - 已解决问题在后续版本以同一技术签名重现时标为回归；
    - 相同“没反应”文字但异常、执行阶段或合同冲突不同，不得自动合并；
    - 缺少完整技术签名时不得仅凭文本相似自动合并。
12. “保守聚合”以上述用例为准，不允许退化成每次相同 Bug 报告都创建不同 Problem。若 qualified Bug 在真实
    样例中经常无法生成完整签名，应补强 Evidence 投影或支持的 signature kind，而不是把重复问题当作正常结果。

### 普通用户固定回执

13. Bug / 深度 unknown 的终局回执由模型外固定模板生成，不只规定文风，也不交给 Answer LLM 自由组织：
    - 新 Bug：`确认这是一个 Bug，已记录为 {problem_id}，请等待主人解决。`
    - 已有 Bug：`确认这是已记录的问题 {problem_id}，本次发生已经关联，请等待主人解决。`
    - 深度 unknown：`暂时无法判断是不是 Bug，但已经记录为 {record_id}，请等待主人确认。`
14. 只有 Problem / investigation 与 Report / Occurrence 已成功原子写入后才能发送“已记录”或“已关联”。写入失败
    时使用固定失败回执，不得伪造编号或成功状态：`已经完成判断，但问题记录暂时失败，请联系主人处理。`
15. 固定回执不包含 ProblemSignature、源码、配置键值、群名单、日志、Evidence ID、责任候选或聚合依据。
    `not_bug / teach_correction` 仍复用公开 Guidance，允许根据当前问题给出具体正确用法。

## 理由

- 展示文字需要可读和可编辑，技术身份需要稳定、可复算和可审计，两者不能共用 LLM 自由文本；
- 版本化部分指纹允许不同 Evidence 类型使用最小、适合自身的稳定字段，也允许算法演进而不静默改写历史；
- 每种 signature kind 只要求少量必要字段，避免为记录完整而增加聊天、平台或源码读取；
- 对常见同根因案例设置正向聚合验收，避免“宁可拆分”退化为每次都新建问题；
- 模型外固定回执是持久化事务结果，不需要 LLM 组织语言，且更容易保证不泄漏内部调查细节。

## 带来的影响

- 需要新增 ProblemSignature、签名构建器、算法版本与匹配结果，但不新增远端服务或向量数据库；
- Bug Agent output / Prompt 是否需要增加受限 `cause_kind` 要通过新资格评测确认；不能让签名构建器读取自由
  推理文本；
- 当前 request digest 不能继续充当 failure signature 或 Problem key；
- Problem title 可以由 Agent 生成并随维护者编辑，不触发 identity 迁移；
- 聚合 correctness 需要独立领域 Fixture，并在 Agent 新字段接线后使用全新的 forward-heldout 验证；
- 自动外部 Issue、通知或修复仍不在范围内。

## 没有采用的方案

### 让 LLM 直接选择已有 Problem

没有采用。自然语言相似、Prompt 变化和上下文差异无法提供稳定身份，也难以模型外审计。

### 只用 request 文本或 embedding 聚类

没有采用。相同根因可以有不同说法，相同“没反应”也可能来自不同责任层；语义相似只适合作为维护者候选。

### 每次无法证明时都算作正常重复问题

没有采用。无法签名时允许独立记录，但第一版必须通过同根因不同说法等正向聚合用例；若真实命中率过低，
需要补强签名类型而不是接受永久重复建档。

## 与既有决定的关系

- [ADR-0078](0078-defer-persisting-unknown-bug-assessments.md) 暂缓深度 `unknown` 的聚合和“已记录”
  回执；确定 `bug` 的版本化 Evidence 指纹继续有效；

- 补充 [ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md) 的 Problem identity 与版本边界；
- 补充 [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 的自动记录和用户回执；
- 保留 [ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md) 对 verified verdict 精确适用性的现行要求；
  Problem 聚合相同不代表旧 verdict 可以跨 revision 无条件短路；
- 采用 Sentry fingerprinting 与 SARIF versioned partial fingerprints 的工程形状，但 signature kind、Evidence
  门禁和失败语义由 Triage 自己拥有；
- [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) 进一步固定中性公开 ID、事务回执和
  首版维护命令，并暂缓“继续观察 / 忽略”。

## 相关文档

- [ADR-0070：分离 Bug Report、Occurrence 与 Problem](0070-separate-bug-reports-occurrences-and-problems.md)
- [ADR-0068：把合格 Agent 的 Bug verdict 作为正式判断并由人工事后监督](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)
