# ADR-0007：采用单发行包与插件、领域核心双命名空间

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-09 |

## 背景

项目首次公开发布前需要同时提供符合 NoneBot 社区惯例的顶层插件入口，以及不依赖 QQ / NoneBot
传输类型的领域核心。当前插件入口嵌套在原领域包中，NoneBot 观察器、UniSeg 引用桥和 OneBot 出站
Provider 也与领域代码混放；若直接发布，使用者会依赖不典型加载路径，核心边界也难以从包结构核对。

当前插件与核心仍由同一作者、同一版本和同一套测试共同演进，领域接口尚无第二个独立消费者。此时拆成
两个仓库或两个发行包会提前引入配对版本、跨仓集成测试和双发布成本。

## 决策

1. 产品品牌采用 **NoneBot Triage Agent**，仓库和 PyPI 发行名采用 `nonebot-plugin-triage`；
2. 一个仓库只构建一个 wheel，同时发布两个顶层 Python 命名空间：
   - `nonebot_plugin_triage`：唯一规范 NoneBot 插件入口，拥有插件配置、生命周期观察、UniSeg 引用转换、
     适配器出站 Provider 和群聊 Matcher；
   - `nbtriage`：领域核心和 CLI，不能导入 QQ / NoneBot 传输类型；
3. 项目尚未发布，不保留原发行名、CLI、配置前缀或嵌套插件入口的兼容别名，避免两个入口重复注册全局
   hook；
4. CLI 和配置前缀采用 `nbtriage`；对外产品说明可以使用 “NoneBot Triage Agent”；
5. 只有出现 NoneBot 之外的第二个真实宿主并通过同一领域契约与评测后，才讨论抽取通用 `bottriage` /
   `bottriage-agent` 发行包或拆分仓库。

## 选择理由

- 顶层插件入口符合 NoneBot 使用者的安装与加载心智模型；
- 包结构直接约束入口依赖领域核心，而不是让领域核心反向依赖框架和适配器；
- 单 wheel 保留原子改动、一次 CI 和一个版本，当前不会产生跨包兼容矩阵；
- 延迟通用化能够让第二个真实消费者证明抽象，而不是为了名称预先设计未经验证的框架。

## 代价与限制

- 构建和隔离安装测试必须同时验证两个顶层命名空间都进入 wheel；
- 插件边缘与领域核心之间需要显式转换 Target、Reply、Event 等框架类型；
- 当前 `nbtriage` 仍是面向 NoneBot 场景验证的核心，不能仅凭传输无关类型宣称已经跨 Bot 框架复用；
- 首次公开发布前的全量重命名没有兼容层，任何本地脚本都必须同步迁移。

## 落实与确认

- 实施情况：已落实。构建产物同时包含 `nbtriage` 与 `nonebot_plugin_triage`，隔离加载和双命名空间
  内容检查均已通过。
- ADR-0016 后续收紧了本 ADR 中的 CLI 部分：`nbtriage` 继续作为发行包领域核心，但不再安装 console
  script；维护 CLI 已迁入不进入 wheel 或 sdist 的仓库 `tools/`。单发行包与双运行时命名空间的决定不变。

## 相关文档

- [架构概览](../architecture/overview.md)
- [ADR-0006：跨平台 Alconna 入口与引用 Provider](0006-cross-platform-alconna-entry-and-reference-providers.md)
- [ADR-0016：将维护者评测工具排除在插件安装面之外](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md)
