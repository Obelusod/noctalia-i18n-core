# Noctalia i18n Core

[English](https://github.com/Obelusod/noctalia-i18n-core/blob/main/README.md)

Noctalia i18n Core 是一个非官方、提供完整类型信息的 Python 库，用于从 [Noctalia Translate](https://i18n.noctalia.dev/projects) 采集翻译变更、持久化投递状态、渲染调用方自有的 Discord 文案并可靠投递。要求 Python 3.12 或更高版本。

## 功能特点

- 使用与项目绑定的不透明游标采集规范化的新增、修改和删除。
- 不设置任意页数上限，获取已存游标之后仍可访问的全部变更。
- 一次数据源采集由多条独立路由共享，各路由拥有自己的 locale、操作、文案和投递策略。
- 使用 SQLite 持久保存原文快照、路由 outbox、投递回执和基线通知状态。
- 将重复变更折叠为净结果，并按调用方选择的阈值合并较大的 locale 批次。
- 等待活动趋于平静后投递并限制最长延迟，也可显式强制投递待处理内容。
- 在不修改状态、不联系 Discord 的情况下预览最终消息。
- 校验外部 YAML 模板、Discord Embed 限制以及数据源与状态的 JSON 边界。
- 遵循 Discord 限流要求，错误信息不会泄露 Webhook URL。

应用负责提供配置、凭据、调度、日志、HTTP session 配置和文案文件。

## 项目结构

```text
noctalia_i18n_core/
├── sources/            # 翻译变更数据源适配器
│   └── noctalia.py     # Noctalia Translate 数据源适配器
├── diff.py             # 多语言 ANSI Diff 渲染
├── discord.py          # Discord 路由、渲染与投递
├── messages.py         # YAML 文案加载与渲染
├── models.py           # 共享监控领域值
├── monitor.py          # 监控流程与投递策略
└── state.py            # SQLite 检查点与投递状态
```

## 安装

从 PyPI 安装：

```bash
pip install noctalia-i18n-core
```

使用 uv：

```bash
uv add noctalia-i18n-core
```

## 快速上手

本库不内置文案文件。以下示例加载调用方自有模板并运行一条 Discord 路由：

```python
from contextlib import closing
from pathlib import Path

import requests

from noctalia_i18n_core import (
    DeliveryPolicy,
    DiscordNotifier,
    DiscordRoute,
    DiscordWebhookSender,
    Monitor,
    NoctaliaSource,
    SQLiteState,
    load_merge,
    load_message,
)

message_root = Path("/etc/my-app/messages")
source_message = load_message("english", message_root / "source")
target_message = load_message("english", message_root / "target")
merge_message = load_merge("english", message_root / "merge")

route = DiscordRoute(
    id="default",
    target_ref="default",
    monitor_id="noctalia",
    project="noctalia",
    locales=frozenset({"en", "zh-Hans"}),
    actions=frozenset({"added", "modified", "deleted"}),
    delivery=DeliveryPolicy(
        quiet_seconds=240,
        max_wait_seconds=900,
        fold_changes=True,
        merge_threshold=5,
    ),
    source_renderer=source_message,
    target_renderer=target_message,
    merge_renderer=merge_message,
)

with (
    requests.Session() as session,
    closing(SQLiteState(Path("state.sqlite3"))) as state,
):
    source = NoctaliaSource("noctalia", timeout=30, session=session)
    sender = DiscordWebhookSender(
        session,
        {"default": "https://discord.com/api/webhooks/..."},
        timeout=30,
    )
    monitor = Monitor(
        source,
        state,
        DiscordNotifier((route,), sender),
        retention_days=180,
    )
    monitor.run()
```

首次运行只建立基线，不重放现有历史。后续运行会先原子推进游标并将匹配变更写入 outbox，再执行投递。每次成功的 Discord 请求只确认其中包含的记录，因此后续请求失败时，其余 outbox 仍会完整保留。

## 监控生命周期

```python
monitor.run()                 # 采集并投递已成熟批次
monitor.run(flush=True)       # 同时投递尚未成熟的待处理批次
preview = monitor.preview()   # 采集和渲染，但不写入或发送
monitor.reset("baseline")     # 替换数据源基线；保留投递状态
monitor.reset("full")         # 清空全部监听状态；建立新基线
```

`run()` 在尝试投递前持久保存新发现的变更，因此待处理记录可在重启和传输故障后继续处理，投递回执则会避免重复发送已成功的请求。

`preview()` 会读取所选数据源，但不会写入 SQLite、调用 sender 或建立缺失的基线。`reset()` 默认抑制新的基线通知，仅在传入 `notify=True` 时发送。

`SQLiteState.summary()` 提供初始化状态、更新时间、原文快照大小、回执数、基线通知数以及待投递记录数和路由数。

## 数据源

`NoctaliaSource(project, timeout, session=None)` 读取 Noctalia Translate 的结构化 Recent Changes 数据和英文导出。项目标识使用小写字母、数字及单个连字符，例如 `noctalia`、`official-plugins` 和 `community-plugins`。

- `poll(None)` 返回最新游标及完整英文原文快照，不重放历史。
- `poll(cursor)` 返回按时间从旧到新排列的唯一变更，并遍历所有已报告历史页直至找到上一事件。
- 其他数据源或项目的游标会明确失败，不会静默建立新基线。
- `history(page)` 按上游原生的从新到旧顺序返回一页历史。
- `close()` 仅关闭数据源自行创建的 session；传入的 session 始终归调用方所有。

数据源游标是 JSON 值，但有意保持不透明；调用方必须原样持久化并交还。

自定义数据源需实现 `Source` 并返回 `PollResult`，两者均从 `noctalia_i18n_core` 导入。首次采集必须提供完整原文语言映射；后续采集若返回的规范化英文变更足以推进已有快照，则可省略完整映射。

## 路由与投递

`DiscordRoute` 绑定变更订阅、渲染器、投递策略和不透明的 `target_ref`。`DiscordWebhookSender` 通过调用方提供的目标映射解析该引用，使凭据不会进入持久状态或预览结果。

| 字段 | 用途 |
| --- | --- |
| `id` | 在一个 notifier 内唯一的稳定路由标识 |
| `target_ref` | 由 sender 解析的不透明键 |
| `monitor_id` | 暴露给模板的调用方监听器标识 |
| `project` | 暴露给模板的调用方项目标识 |
| `locales` | 精确 locale 集合，或表示全部 locale 的 `frozenset({"*"})` |
| `actions` | `added`、`modified`、`deleted` 的非空子集 |
| `delivery` | 路由自己的累积与合并策略 |
| `source_renderer` | 英文原文变更的渲染器 |
| `target_renderer` | 目标 locale 变更的渲染器 |
| `merge_renderer` | 可选的 locale 合并批次渲染器 |
| `baseline_renderer` | 启用基线通知的可选渲染器 |
| `username` | 可选的单次请求 Discord 用户名覆盖值 |
| `avatar_url` | 可选的单次请求 Discord 头像覆盖值 |

locale 通配符必须单独使用。订阅英文的路由必须提供 `source_renderer`，订阅任意目标 locale 的路由必须提供 `target_renderer`；仅当启用合并时才必须提供 `merge_renderer`。提供 `baseline_renderer` 时，它会收到近期变更数和原文数。

`DeliveryPolicy` 控制每条路由处理 outbox 的时机与方式：

| 字段 | 用途 |
| --- | --- |
| `quiet_seconds` | 自动投递前需要连续无新变更的秒数 |
| `max_wait_seconds` | 最早待处理记录允许存在的最长时间 |
| `fold_changes` | 是否将同一 locale 与键的历史折叠为净变更 |
| `merge_threshold` | 批次大小超过此值时合并同一 locale；`None` 禁用合并 |

`merge_threshold` 为 `0` 时会合并每个非空批次。自定义传输需实现 `DiscordSender`，其 `send(target_ref, payload)` 方法接收完整且与 JSON 兼容的 Discord payload。

## 文案模板

本库不内置也不选择文案文件。`load_message(name, directory)` 和 `load_merge(name, directory)` 解析 `<directory>/<name>.yaml`，并拒绝不安全名称、重复 YAML 键、缺失或未知字段、非法占位符及超出 Discord Embed 限制的内容。

逐条文案必须提供 `added`、`modified`、`deleted` Embed，并可定义 `diff`：

````yaml
diff:
  old: {color: red, bold: true, underline: false}
  new: {color: green, bold: true, underline: false}
added:
  title: "[{locale}] 新增"
  description: |-
    {key_link}
    `{new_value:truncate=1000}`
modified:
  title: "[{locale}] 修改"
  url: "{change_url}"
  description: |-
    ```ansi
    − {old_diff}
    + {new_diff}
    ```
deleted:
  title: "[{locale}] 删除"
  description: "{key_link}"
````

合并文案必须提供 `source`、`target` 和 `entries`；`entries` 必须提供 `separator`、`added`、`modified`、`deleted`，可选的 `diff` 使用与逐条文案相同的结构。超大合并消息只会在完整条目边界拆分。

Embed 模板支持 `title`、`description`、`url`、`timestamp`、`color`、`footer`、`image`、`thumbnail`、`author` 和 `fields`。逐条 Embed 可设置 `url: "{change_url}"`，使标题链接到对应变更。合并 Embed 不提供 `change_url`，因为一个批次可能包含多项变更。

逐条 Embed 与合并条目支持以下占位符：

| 占位符 | 内容 |
| --- | --- |
| `{monitor_id}` | 调用方定义的监听器标识 |
| `{project}` | 调用方定义的项目标识 |
| `{key}` | 翻译键 |
| `{key_link}` | 链接到变更页面的键；链接不可用时使用行内代码样式 |
| `{source}` | 当前英文原文 |
| `{old_value}` | 修改或删除前的值 |
| `{new_value}` | 新增或修改后的值 |
| `{old_diff}` | 使用已配置 ANSI 样式标记变更词元的旧值 |
| `{new_diff}` | 使用已配置 ANSI 样式标记变更词元的新值 |
| `{locale}` | 规范化 locale 标识 |
| `{actor}` | 编辑者名称 |
| `{actor_url}` | 编辑者资料页地址（如可用） |
| `{actor_avatar_url}` | 编辑者头像地址（如可用） |
| `{action}` | `added`、`modified` 或 `deleted` |
| `{change_url}` | 翻译编辑页面地址（如可用） |
| `{timestamp}` | UTC ISO 8601 变更时间 |
| `{unix_time}` | 可用于 Discord 时间戳标记的 Unix 秒数 |

合并 Embed 支持以下批次占位符：

| 占位符 | 内容 |
| --- | --- |
| `{monitor_id}` | 调用方定义的监听器标识 |
| `{project}` | 调用方定义的项目标识 |
| `{locale}` | 批次共有的 locale |
| `{count}` | 当前消息包含的变更数 |
| `{actors}` | 去重后的编辑者名称 |
| `{actor_count}` | 编辑者人数 |
| `{actor_avatar_url}` | 批次只有一名编辑者且头像可用时的头像地址 |
| `{added_count}` | 新增数量 |
| `{modified_count}` | 修改数量 |
| `{deleted_count}` | 删除数量 |
| `{first_timestamp}` | 第一项变更的 UTC ISO 8601 时间 |
| `{last_timestamp}` | 最后一项变更的 UTC ISO 8601 时间 |
| `{first_unix_time}` | 第一项变更的 Unix 秒数 |
| `{last_unix_time}` | 最后一项变更的 Unix 秒数 |
| `{entries}` | 使用 `entries.separator` 连接的完整条目 |

使用 `truncate=N` 限制 `{key}`、`{source}`、`{old_value}`、`{new_value}` 和 `{actor}`；合并 Embed 还允许用于 `{actors}` 和 `{entries}`。`N` 包含省略号，宽字符按两列计算：

```yaml
value: "{source:truncate=1024}"
```

使用 `fallback=...` 替换不可用的字符串值：

```yaml
icon_url: "{actor_avatar_url:fallback=https://github.com/noctalia-dev.png}"
```

仅当使用 `{old_diff}` 或 `{new_diff}` 时才必须定义 `diff`。ANSI 颜色可填写 `gray`、`red`、`green`、`yellow`、`blue`、`magenta`、`cyan`、`white` 或 `null`；`bold` 和 `underline` 分别控制强调样式。

## API 契约

面向调用方的受支持名称均直接从 `noctalia_i18n_core` 导出；子模块只用于组织实现，正常导入无需依赖其路径。`JsonValue` 描述不透明的 JSON 形状游标与预览数据，JSON 校验及规范化保持内部使用。

非法构造参数和模板会抛出 `ValueError`；数据源、SQLite、渲染及传输故障会抛出 `RuntimeError`。本库不定义自有异常层级。

`SQLiteState` 在调用 `close()` 前持有其数据库连接。`NoctaliaSource` 只关闭自行创建的 session；`DiscordWebhookSender` 不会关闭或重新配置传入的 session。

## 开发

安装锁定的开发环境：

```bash
uv sync --locked
```

检查格式、代码规则、静态类型并运行全部测试：

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run python -m unittest discover -v
```

仅运行一个测试模块：

```bash
uv run python -m unittest tests.test_noctalia -v
```

## 构建

构建 wheel 与源码包，然后校验其包元数据：

```bash
uv build --no-sources --clear
uvx twine check --strict dist/*
```

## 许可证

[MIT](https://github.com/Obelusod/noctalia-i18n-core/blob/main/LICENSE)
