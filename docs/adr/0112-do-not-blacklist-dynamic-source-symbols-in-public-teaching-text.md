# ADR-0112：公开教学文本不使用动态源码符号黑名单

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-22 |

## 当时遇到了什么

公开投影曾把每轮 Evidence ID、locator、配置源码符号和部分 locator 函数名动态加入字符串黑名单，
并额外拒绝一组实现与框架术语。匹配采用任意子串包含关系，因此匿名 Handler `_` 会拒绝
`course_help`、`show_today` 等合法命令，导致已经生成的完整候选反复 correction，最终以预算或 Provider
错误结束。

对版本化评测报告、显式真实插件诊断中的投影前 `final_result` 和本地活动教学制品进行审计后，没有发现
当前 Prompt 输出 Evidence ID、源码 locator、路径或 revision。早期合同曾输出 `OWNER`、`MEMBER` 和
`Option` 等框架词，但它们属于公开措辞质量问题，不是密钥或私有配置泄露。原黑名单从教学功能首次实现
时即作为预防措施存在，没有对应的真实泄露事故。

## 决定

1. 移除根据 Evidence ID、locator、配置源码符号和函数名生成的动态公开文本黑名单，也不再模型外硬拒绝
   固定的实现或框架术语。
2. Prompt 继续要求模型不得公开源码路径、Python 符号、配置键、环境变量、Evidence ID、框架实现词和
   分析过程措辞；这属于公开文案职责，不再伪装成安全边界。
3. 真实保密边界继续在模型输入和公开投影结构上执行：未授权配置、密钥与 `.env` 不进入 Evidence；动态
   Evidence 必须来自批准根并绑定 revision；公开 Help / Answer 只消费教学 entry 字段，不复制源码正文或
   locator。
4. 公开投影继续验证文本长度和控制字符、usage 结构、Runtime 命令与 alias、Evidence 引用闭包、配置引用、
   requirement 元数据及其他确定性合同。
5. 将来只有观察到真实模型原始提交反复泄露某种机器工件时，才增加能精确描述该工件的校验或 correction；
   不再从任意插件源码标识符生成开放式子串黑名单。

## 为什么这样选

- 动态源码符号既可能是内部实现名，也可能与合法命令、占位名或自然语言重合，无法用子串比较可靠区分。
- 字符串黑名单不能阻止未知密钥或源码字面量泄露；让敏感值不进入模型才是有效的保密边界。
- 当前 Prompt 在投影前原始输出中已经遵守公开措辞约束，继续保留硬拒绝只增加误杀、无效 correction、
  token 消耗和单元失败。
- usage、Evidence、配置和权限仍由结构化校验负责，不会因删除文案黑名单而放宽这些合同。

## 带来的影响

- `course_help`、`show_today` 等包含下划线的合法命令不会再因匿名函数 `_` 被拒绝。
- 模型若偶尔使用框架术语，会作为教学文案质量问题进入后续真实插件诊断或 held-out 评测，而不是被不透明
  的通用黑名单直接关闭整个单元。
- 公开 schema、Prompt 和 request payload 均未改变，因此保持 schema 8、Prompt v56 和 request v23；
  当前合同仍没有可继承的正式 Provider 资格，下一次 held-out 必须使用包含本决定的当前实现。

## 落实与确认

- `capability_annotations.validate_capability_public_statement()` 只承担通用公开文本约束；
- 公开投影不再读取 request 的源码符号生成禁词；
- 回归测试使用匿名 Handler locator 与 `show_today` 命令复现原误杀，并继续锁定 Prompt 的公开措辞要求。

## 相关文档

- [教学注释使用确定性 Evidence 与有界导航](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [简化公开能力教学合同](0094-simplify-the-public-capability-teaching-contract.md)
- [完整捕获显式维护运行的模型输出](0097-capture-complete-capability-model-output-in-explicit-maintenance-runs.md)
- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
