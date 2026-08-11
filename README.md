<div align="center">

<a href="https://v2.nonebot.dev/store">
  <img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-template/refs/heads/resource/.docs/NoneBotPlugin.svg" width="310" alt="NoneBot Plugin">
</a>

# NoneBot Triage Agent

[![License](https://img.shields.io/github/license/Misty02600/nonebot-plugin-triage.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11--3.14-blue.svg)](https://www.python.org/)
[![NoneBot](https://img.shields.io/badge/NoneBot-2.5+-ea5252.svg)](https://nonebot.dev/)
[![CI](https://github.com/Misty02600/nonebot-plugin-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/Misty02600/nonebot-plugin-triage/actions/workflows/ci.yml)

把群聊中的显式报障关联到 NoneBot 本机运行证据。

</div>

## 介绍

用户回复一条近期消息并发送 `@Bot 报错` 后，插件会尝试关联这条消息在本机产生的事件、Matcher、API
调用和异常记录，并返回一个受理编号。维护者可以通过编号查看简短摘要。

插件默认不调用模型，不执行报障文本里的命令，也不会自动创建 Issue。运行观察、消息引用和受理记录目前
只保存在单进程内存中，Bot 重启后会丢失。

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
| `NBTRIAGE_REPORT_COMMAND`      | `报错`                       | 普通用户报障命令                  |
| `NBTRIAGE_QUERY_COMMAND`       | `报错查询`                   | 维护者查询命令                    |
| `NBTRIAGE_FEEDBACK_COMMAND`    | `报错反馈`                   | 维护者反馈命令                    |
| `NBTRIAGE_TRIAL_STATS_COMMAND` | `报错统计`                   | 维护者试运行统计命令              |
| `NBTRIAGE_TRIAL_MODE`          | `off`                        | 可设为 `observe` 开启本地观察日志 |
| `NBTRIAGE_TRIAL_LOG_PATH`      | `logs/nbtriage-trials.jsonl` | `observe` 模式的本地日志路径      |
| `NBTRIAGE_MODEL_ENABLED`       | `false`                      | 当前请保持关闭                    |


## 使用

所有命令都需要 `@Bot`。

| 指令                                         | 权限      | 说明                   |
| -------------------------------------------- | --------- | ---------------------- |
| 回复近期消息并发送 `报错`                    | 所有人    | 建立受理记录并返回编号 |
| `报错查询 <受理编号>`                        | SUPERUSER | 查看短期运行摘要       |
| `报错反馈 <受理编号> <有用\|不完整\|不正确>` | SUPERUSER | 为观察型试运行记录反馈 |
| `报错统计`                                   | SUPERUSER | 查看当前试运行统计     |

跨平台入口由 Alconna / UniSeg 提供。当前只有 OneBot V11 实现了 Bot 出站消息的精确引用关联；其他适配器
可以提交报障，但不保证能关联 Bot 主动发送的消息。

## 许可证

本项目使用 [MIT License](LICENSE)。
