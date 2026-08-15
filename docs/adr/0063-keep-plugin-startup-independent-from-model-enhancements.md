# ADR-0063：让插件启动独立于模型增强

- 状态：已采纳
- 决策日期：2026-08-15

## 背景

NoneBot 商店检查与普通首次安装不会提供部署者的模型 backend、模型名称或私有 API Key。教学注释此前在
插件运行时组装阶段被当作强制链路，默认配置会在 NoneBot 导入插件时抛出配置错误。这会让可选的网络模型
增强反过来阻断确定性 Matcher、能力索引和基础命令，也使商城检测依赖不可能提供的私有凭据。

## 决定

1. 插件导入、Matcher 注册和确定性能力索引不得依赖模型 transport 或 API Key。
2. backend/model 均未配置时，不组装教学注释 client；semantic assessment 与公开 Answer 使用 unavailable
   service，确定性索引继续工作。
3. backend/model 已配置但密钥缺失、任务资格不匹配或专用运行合同不满足时，只禁用对应模型增强并记录不含
   配置值或密钥的降级日志，不阻断插件启动。
4. backend 与 model 只配置一项仍是部署配置错误，由 `NBTriageConfig` 在插件装配前拒绝；这不是模型网络
   可用性降级。
5. 模型请求阶段的错误密钥、网络故障、超时或非法输出继续在各子服务内部失败关闭；不得清除确定性事实、
   发布不完整注释或使 Bot 退出。
6. 不恢复 `enabled` 或 annotation mode。模型增强是否可用由成对 transport 配置、任务资格和运行凭据共同
   派生；没有额外产品开关。

## 影响

- 商城式干净环境可以在没有模型配置和私有 Key 时导入插件；
- 此时 `triage` 的模型意图分类与自然语言 Answer 不可用，教学注释不会刷新，但确定性 capability shadow、
  已声明的公开帮助事实和其他非模型功能仍可初始化；
- 配置了模型但配置错误时，日志会明确指出对应增强 unavailable，部署者可修正后重启；日志不包含异常原文、
  Key 或配置值；
- 本决定部分替代 ADR-0058 中“模型 transport 不合格即启动失败”的启动策略，不改变其 Evidence、导航、
  字段所有权、缓存和发布边界。

## 验证

- 使用没有 `NBTRIAGE_MODEL_*` 与 `OPENCODE_API_KEY` 的独立工作目录和子进程执行 NoneBot 初始化与插件加载；
- 保留教学注释 strict factory 的单元测试，确保已配置路径仍执行精确任务资格检查；
- 验证公开 Answer 在缺少密钥时返回 transport unavailable，而不是让插件组装失败。
