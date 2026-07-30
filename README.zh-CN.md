# Noctalia i18n Core

[English](https://github.com/Obelusod/noctalia-i18n-core/blob/main/README.md)

Noctalia i18n Core 采集 [Noctalia Translate](https://i18n.noctalia.dev/projects) 的规范化翻译变更，并使用 SQLite 持久化采集位置、源文案快照、待投递记录和投递回执。调用方通过路由筛选变更，使用任意同步或异步传输发送成熟批次，并在成功后确认。

## 功能

- 通过与项目绑定的不透明游标采集规范化的新增、修改和删除。
- 不设置任意页数上限，恢复存储游标之后所有仍可获取的变更。
- 在具有独立 locale、action 和投递策略的多条路由间共享一次数据源轮询。
- 使用 SQLite 持久化源文案快照、路由 outbox、投递回执和基线回执。
- 将重复变更折叠为净结果，并等待活动平息或最长等待时间到达后投递。
- 在不修改状态的情况下预览当前周期或强制刷新将产生的投递。
- 校验数据源与状态边界，同时将游标表示限制在对应适配器内部。

应用负责配置、凭据、调度、日志、HTTP session 配置、路由、渲染和传输。

## 项目结构

```text
noctalia_i18n_core/
├── sources/            # 翻译数据源协议与适配器
│   └── noctalia.py     # Noctalia Translate 适配器
├── models.py           # 共享领域值
├── monitor.py          # 采集、路由、折叠与批次策略
└── state.py            # SQLite 检查点与投递状态
```

## 安装

使用 uv 从 PyPI 安装：

```bash
uv add noctalia-i18n-core
```

或使用 pip：

```bash
pip install noctalia-i18n-core
```

## 使用指南

下面的示例监听简体中文变更，将成熟批次输出到终端，并在输出成功后确认对应记录：

```python
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from noctalia_i18n_core import (
    Change,
    DeliveryPolicy,
    Monitor,
    NoctaliaSource,
    SQLiteState,
)


@dataclass(frozen=True, slots=True)
class ChineseRoute:
    id: str = "zh-Hans"
    delivery: DeliveryPolicy = DeliveryPolicy(
        quiet_seconds=60,
        max_wait_seconds=600,
        fold_changes=True,
    )
    notify_baseline: bool = False

    def accepts_locale(self, locale: str) -> bool:
        return locale == "zh-Hans"

    def matches(self, change: Change) -> bool:
        return self.accepts_locale(change.locale)


with (
    closing(NoctaliaSource("noctalia", timeout=30)) as source,
    closing(SQLiteState(Path("state.sqlite3"))) as state,
):
    monitor = Monitor(
        source,
        state,
        (ChineseRoute(),),
        retention_days=180,
    )
    result = monitor.run()

    for route_id in result.baseline_routes:
        print(route_id, result.scanned, result.source_texts)
        state.acknowledge_baseline(route_id)

    for route_id, deliveries in result.deliveries.items():
        for delivery in deliveries:
            change = delivery.change
            print(route_id, change.action, change.key, change.new_value)
        state.acknowledge(route_id, deliveries)
```

首次运行建立当前位置的基线，不重放已有历史。之后由应用的调度机制重复运行相同流程：监控器先持久化新游标和匹配变更，再返回已满足等待策略的批次。示例一次处理整个批次；真实传输应在每次外部请求成功后，只确认该请求实际包含的 `Delivery`。

`Monitor` 不负责循环运行、渲染或传输。同步应用可以直接处理结果。异步应用可以在工作线程中短暂打开数据源与状态库并运行监控，在事件循环中发送结果，再在工作线程中重新打开状态库完成确认；同一个 `NoctaliaSource` 或 `SQLiteState` 实例不应跨线程使用，同一状态文件的采集与重置周期必须串行执行。

## 数据源

`NoctaliaSource(project, timeout, session=None)` 读取 Noctalia Translate 的结构化 Recent Changes 数据和英文导出。项目标识符使用小写字母、数字和单个连字符，例如 `noctalia`、`official-plugins` 和 `community-plugins`。

- `poll(None)` 返回最新游标和完整英文源文案快照，不重放历史。
- `poll(cursor)` 返回所有仍可获取的新变更，并沿上游报告的历史页查找前一事件。
- 属于其他数据源或项目的游标会明确失败，不会静默创建新基线。
- `history(page)` 按上游原有的从新到旧顺序返回一页记录。
- `close()` 只关闭数据源自行创建的 session；调用方传入的 session 仍归调用方所有。

数据源游标是 JSON 值，但有意保持不透明。调用方必须原样持久化并传回。

自定义数据源实现 `Source` 并返回 `PollResult`。首次轮询必须包含完整源语言映射；后续轮询在规范化英文变更足以推进存储快照时可以省略该映射。

如果只需要采集而不需要持久监控，可以直接调用数据源：

```python
from contextlib import closing

from noctalia_i18n_core import NoctaliaSource

with closing(NoctaliaSource("noctalia", timeout=30)) as source:
    baseline = source.poll(None)
    result = source.poll(baseline.cursor)

for change in result.changes:
    print(change.locale, change.action, change.key)
```

实际应用应持久化游标，并在下一次运行时原样传回。后续结果包含按发生时间从旧到新排列且不重复的变更。

## 监控

`Monitor` 组合 `Source`、状态存储和一组路由：

```python
result = monitor.run()                    # 采集并返回已成熟的批次
result = monitor.run(flush=True)          # 同时返回尚未成熟的批次
preview = monitor.preview()               # 预览当前周期，不写入状态
preview = monitor.preview(flush=True)     # 预览强制刷新结果
result = monitor.reset("baseline")        # 替换基线并保留投递状态
result = monitor.reset("full")            # 清空投递状态并建立新基线
```

`run()` 原子推进数据源检查点并将匹配变更写入 outbox，再通过 `MonitorResult.deliveries` 返回成熟批次。待处理记录会跨重启和传输失败保留；应用使用状态存储的 `acknowledge(route_id, deliveries)` 确认成功请求后，投递回执会防止相同变更再次进入同一路由。

`preview()` 合并已存储的待处理记录与新发现的变更，再应用与 `run()` 相同的等待、筛选和折叠策略，但不修改状态。传入 `flush=True` 可以预览强制刷新结果。

`MonitorResult.baseline_routes` 列出尚未确认的基线通知路由。应用成功发送后调用状态存储的 `acknowledge_baseline(route_id)`；未确认的路由会在后续周期再次返回。`reset()` 默认将新基线视为无需通知，传入 `notify=True` 才会返回相应路由。`baseline` 模式保留 outbox 和回执状态；`full` 在建立新基线前清空全部监控状态。

## 路由与投递

`Route` 是包含以下成员的结构化协议：

| 成员 | 用途 |
| --- | --- |
| `id` | 稳定路由标识符 |
| `delivery` | 路由独立的 `DeliveryPolicy` |
| `notify_baseline` | 是否接收基线通知 |
| `accepts_locale(locale)` | 是否订阅指定 locale |
| `matches(change)` | 是否接受规范化变更 |

路由 ID 在一个监控周期内必须非空且唯一。它标识持久订阅而非传输地址；应使用稳定且不含凭据的值，不应使用可变显示名称或 Webhook URL。

`Monitor` 在构造时保存路由快照。应用的持久订阅发生变化后，应使用最新路由创建新的监控器；下一次 `run()` 会清理已移除路由的 outbox 和基线回执。

`DeliveryPolicy` 控制 outbox 准备行为：

| 字段 | 用途 |
| --- | --- |
| `quiet_seconds` | 自动投递前所需的无活动时间 |
| `max_wait_seconds` | 最早待处理记录的最长等待时间 |
| `fold_changes` | 是否将同一 locale 和 key 折叠为净变更 |

`max_wait_seconds` 不得小于 `quiet_seconds`。任一值为零时，对应的投递条件会立即满足。

本库不定义传输协议，也不依赖事件循环。调用方可以将同一路由批次拆成多次外部请求，并在每次成功后使用该请求实际包含的 `Delivery` 调用 `acknowledge()`。如果后续请求失败，状态存储会保留所有尚未确认的记录；如果进程在发送成功后、确认前退出，该请求可能在下次运行时重复。这种至少一次语义允许 Webhook、Bot 频道、线程、消息队列和其他传输共享相同的采集与投递状态。

## 状态

`SQLiteState(path, read_only=False)` 持久化：

- 不透明数据源游标；
- 与游标匹配的完整英文快照；
- 按路由划分的待处理投递；
- 投递回执；
- 基线回执。

状态更新使用 SQLite 事务。不兼容的结构会在不修改数据库的情况下被拒绝。只读状态不会写入现有数据库；文件不存在时使用内存中的空状态。

`SQLiteState.summary()` 返回初始化状态、更新时间、源文案快照大小、投递与基线回执数量以及待处理投递和路由数量。成功完成监控周期后，会按照配置的保留期限清理投递回执。

## API 约定

受支持的调用方接口直接从 `noctalia_i18n_core` 导出；子模块仅用于组织实现，常规导入无需依赖子模块。

`JsonValue` 描述不透明的 JSON 结构游标。JSON 校验与规范化保持为内部实现。无效构造参数抛出 `ValueError`；数据源与 SQLite 失败抛出 `RuntimeError`。本库不定义自有异常层次。

`SQLiteState` 在调用 `close()` 前持有其数据库连接。`NoctaliaSource` 只关闭自行创建的 session。

## 开发

安装锁定的开发环境：

```bash
uv sync --locked
```

检查格式、代码规则、静态类型和全部测试：

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run python -m unittest discover -v
```

运行单个测试模块：

```bash
uv run python -m unittest tests.test_noctalia -v
```

## 构建

构建 wheel 与源码包并校验包元数据：

```bash
uv build --no-sources --clear
uvx twine check --strict dist/*
```

## 许可证

[MIT](https://github.com/Obelusod/noctalia-i18n-core/blob/main/LICENSE)
