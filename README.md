<div align="center">

<a href="https://v2.nonebot.dev/store">
  <img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-template/refs/heads/resource/.docs/NoneBotPlugin.svg" width="310" alt="NoneBot Plugin">
</a>

# NoneBot Triage Agent

[![License](https://img.shields.io/github/license/Misty02600/nonebot-plugin-triage.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11--3.14-blue.svg)](https://www.python.org/)
[![NoneBot](https://img.shields.io/badge/NoneBot-2.5+-ea5252.svg)](https://nonebot.dev/)
[![CI](https://github.com/Misty02600/nonebot-plugin-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/Misty02600/nonebot-plugin-triage/actions/workflows/ci.yml)

受理群聊中的显式求助，并按需关联 NoneBot 本机运行证据。

</div>

## 介绍

发送 `triage <求助内容>` 即可调用插件，`@Bot` 可选。`triage` 后可以直接写自然语言，例如询问功能用法，
或描述一次异常。回复近期消息时，插件还会尝试关联这条消息在本机产生的运行记录。

当前默认不调用模型：明确的用法问题会读取已登记的公开 Alconna 能力，明确的故障描述会建立受理记录，
其余请求会追问一次。插件不执行求助文本里的命令，也不会自动创建 Issue。

## 安装

```bash
git clone https://github.com/Misty02600/nonebot-plugin-triage.git
cd nonebot-plugin-triage
uv sync --all-extras --group dev
```

在宿主 NoneBot 项目中加载插件：

```toml
[tool.nonebot]
plugins = ["nonebot_plugin_triage"]
```

## 配置

| 配置项                         | 默认值                       | 说明                              |
| ------------------------------ | ---------------------------- | --------------------------------- |
| `NBTRIAGE_COMMAND`                  | `triage`                     | 普通用户自然语言入口              |
| `NBTRIAGE_REQUEST_MAX_CHARS`        | `2000`                       | 单次求助文字上限                  |
| `NBTRIAGE_SUPPORT_COOLDOWN_SECONDS` | `2`                          | 同一用户连续求助的最短间隔        |
| `NBTRIAGE_REPORT_COOLDOWN_SECONDS`  | `30`                         | 同一用户连续建立故障记录的最短间隔 |
| `NBTRIAGE_QUERY_COMMAND`            | `报错查询`                   | 维护者查询命令                    |
| `NBTRIAGE_FEEDBACK_COMMAND`         | `报错反馈`                   | 维护者反馈命令                    |
| `NBTRIAGE_TRIAL_STATS_COMMAND`      | `报错统计`                   | 维护者试运行统计命令              |
| `NBTRIAGE_TRIAL_MODE`               | `off`                        | 可设为 `observe` 开启本地观察日志 |
| `NBTRIAGE_TRIAL_LOG_PATH`           | `logs/nbtriage-trials.jsonl` | `observe` 模式的本地日志路径      |
| `NBTRIAGE_CAPABILITY_SHADOW_PATH`   | 未设置                       | 可选的本地能力影子 SQLite 路径    |
| `NBTRIAGE_MODEL_ENABLED`            | `false`                      | 当前请保持关闭                    |

## 使用

普通用户入口可以直接发送，也可以 `@Bot` 后发送。维护命令仍需要 `@Bot`。

| 指令                                              | 权限      | 说明                           |
| ------------------------------------------------- | --------- | ------------------------------ |
| `triage 某个功能怎么使用`                         | 所有人    | 说明已登记的公开功能           |
| `triage <能力问题>`（启用能力影子时）             | SUPERUSER | 检索带披露标签的本机能力候选   |
| `triage 刚才执行后报错了`（可回复近期消息）       | 所有人    | 受理故障；Reply 用于关联记录   |
| `@Bot 报错查询 <受理编号>`                        | SUPERUSER | 查看短期运行摘要               |
| `@Bot 报错反馈 <受理编号> <有用\|不完整\|不正确>` | SUPERUSER | 为观察型试运行记录反馈         |
| `@Bot 报错统计`                                   | SUPERUSER | 查看当前试运行统计             |

跨平台入口由 Alconna / UniSeg 提供。当前只有 OneBot V11 实现了 Bot 出站消息的精确引用关联；其他适配器
可以提交求助，但不保证能关联 Bot 主动发送的消息。

### 实验性能力影子索引

如需检查当前部署实际加载了哪些能力候选，可配置一个以 `.sqlite3` 结尾的本地路径：

```dotenv
NBTRIAGE_CAPABILITY_SHADOW_PATH=data/nbtriage-capabilities.sqlite3
```

插件会在启动完成时只读检查已加载的 Plugin、Matcher、Alconna 结构、插件元数据、安装版本和本地源码摘要，
然后原子生成本地全文检索索引。它不调用第三方 Rule、Permission、handler 或命令解析，也不读取 `.env`、
日志和运行数据。未显式公开的第三方能力默认只是 `review` 候选；代表部署开发 / 维护者的 `SUPERUSER`、`CommandMeta.hide=True`
或明确标成内部管理用途的能力会以 `restricted` 写入本地索引，但默认检索不会返回。只有先在模型外确认
当前调用者有权查看的路径，才能检索这部分能力。Token、配置原文和私密日志不是能力，始终从采集源排除；
部署者以后也可以通过独立的 operator exclude policy 在持久化前完全排除某些能力。

源码仓库中的维护命令可以检索该索引：

```bash
just maintainer search-capabilities "搜图怎么用" \
  --index data/nbtriage-capabilities.sqlite3 \
  --include-review
```

本地维护者已经在模型外确认自己有权查看当前部署的内部能力时，可以额外使用 `--include-restricted`。CLI
开关只是声明带外授权，不自行检查身份；群聊中的 `triage <能力问题>` 则会在读取索引前执行 NoneBot
`SUPERUSER` 检查。

这条检索链不依赖模型、网络或向量服务。普通用户仍只看到显式登记的 `public` 能力；SUPERUSER 可以查看
`review` 和 `restricted`，但回复会明确标为未审核候选或维护者可见受限能力。索引缺少可靠用法或存在
不透明规则时不会补写参数，也不会把“发现到”宣称为“当前一定能执行”。启动刷新失败但仍有上一份成功构建
索引时，维护者回复会明确标记快照陈旧；第三方说明中的 mention 和 Unicode 控制字符会在发送前中和。

## 许可证

本项目使用 [MIT License](LICENSE)。
