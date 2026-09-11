# ADR-0123：在每轮教学刷新中共享 ty 定义导航进程

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-10 |

## 当时遇到了什么

Jedi 对部分包内相对导入与内建模块同名的情况无法定位定义，实际影响扩展实现的取证。继续扩大首包或
要求模型猜路径不能修复导入解析。ty 的离线对照验证能解析这些位置，但冷启动时在工作区配置完成前
大量提交请求曾发生挂起。因此后端替换必须同时明确握手、共享与回收，而不能只替换一个 API 调用。

## 决定

- 用固定版本 `ty==0.0.80` 替换 Direct Jedi；不并行维护两种语义后端，不加入完整 LSP 框架。
- `CapabilityAnnotationService.refresh` 拥有一个懒启动会话，初始 Evidence 和模型按需导航共享该进程。
  已有线程执行边界通过请求上下文继承会话；没有语义查询时不启动进程。
- 完成 initialize、工作区配置与该版本的能力注册握手后才放行查询。注册作为就绪标志是 pinned ty 的
  已验证实现约束，不视为所有 LSP 服务端的标准 ready 事件。
- 解析环境绑定当前 Python 环境与导入路径；禁用工作区外部命令与 uv 探测，不执行目标插件代码。
  只实现定义请求、必要通知和生命周期协议，不提供语言服务器任意方法透传。
- 刷新成功、失败或取消后回收进程；下轮新建。进程故障不自动重启，也不缓存为永久的“没有定义”。
  独立调用不保留后台进程，调用结束自行回收。
- 并发打开文件可能让 ty 取消其他查询并返回 LSP `ContentModified`。仅当原查询源码仍未改变时，允许在
  原 15 秒查询时限内最多重发两次；不重试实际取消、其他错误或真实源码漂移，也不重启 Agent/进程。
- 路径准入、source revision、稳定读取、歧义结果与 Evidence 校验继续由 Triage 持有。ty 的位置先通过
  路径门禁，再从目标源码取得名称和类型；导航结果本身不能成为业务事实或可引用正文。
- 支持稳定部署：代码或依赖更新后重启 Bot，再复核或重建教学知识。教学刷新不是热重载；不新增 watcher、
  编辑器同步、跨刷新常驻解析器或任意磁盘变化后的即时一致性保证。发现漂移继续拒绝相关候选。

## 代价与未采用方案

增加一个本地子进程及窄 JSON-RPC 传输层，换取本轮已验证的导入定位能力和并发查询。生命周期、坐标编码
与冷启动需要回归；升级 ty 时需重新验证握手。短生命周期不能替代现有 revision 检查。

不保留 Jedi 回退，不维护其私有 importlib 补丁；不为减少冷启动成本引入跨刷新同步协议。源码发现与动态
注册仍使用已有有界文本搜索，静态导航不承诺解释任意动态 Python 或完整调用图。Bug 工具面不因此扩大。

## 落实与确认

- `src/nbtriage/readonly_tools/python_navigation.py`：后端无关的准入、定位合同与目标元数据校验。
- `src/nbtriage/readonly_tools/ty_navigation.py`：懒启动、握手、多请求响应分发、超时与回收。
- `tests/readonly_tools/test_python_navigation.py`：冷启动并发、相对导入、歧义、stub、Unicode、进程退出、
  取消、源码漂移及路径拒绝；已有教学适配器测试继续验证 Depends 和 gate 取证。
- 请求合同更新为 v70；旧模型资格不能自动继承。离线导航通过不等于新的模型注释已经通过语义验收。

## 替代关系与依据

部分替代 [ADR-0057](0057-select-source-analysis-tools-by-evidence-stage.md) 的 Direct Jedi 后端选择与
[ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md)
中具体导航依赖；其分层、路径门禁及 Provider 可选边界保留。
[ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) 的混合
取证与插件级失效边界不变。

- [ty 编辑器设置](https://docs.astral.sh/ty/reference/editor-settings/)
- [ty 语言服务器能力](https://docs.astral.sh/ty/features/language-server/)
- [所用版本的工作区初始化实现](https://github.com/astral-sh/ruff/blob/e7230cac059fa28bd0e534e4571c3560c695efbe/crates/ty_server/src/session.rs)
