# Noctalia i18n Core

[English](README.md)

[Noctalia Translate](https://i18n.noctalia.dev/) 的非官方 Python 数据访问库，由开发者独立维护。提供带类型的项目进度、译文、待复核项和修订历史查询。支持直接访问网站，以及通过本机或远程 HTTP 数据服务进行查询和增量读取。

## 安装

```bash
uv add noctalia-i18n-core
```

```bash
pip install noctalia-i18n-core
```

要求 Python 3.12 或更新版本。

## 客户端

`WebClient` 和 `DataClient` 分别读取网站及数据服务中的项目。两者共享 `project()`、`keys()`、`catalog()`、`translations()`、`search()`、`missing()`、`reviews()`、`changes()` 和 `translation_url()` 的参数与返回约定。

项目标识均为第一个参数，例如 `WebClient("noctalia")` 或 `DataClient("noctalia", url="https://i18n.obelus.cc")`。两者均支持 `timeout=30`、`session=None` 和 `proxy=None`。

网站客户端提供 `iter_changes()`、`change_pages()` 和 `revision_pages()`。数据客户端提供 `history()`、`revision()`、`contributors()`、`status()` 和 `updates()`，查询范围由服务的采集进度决定。

## 网站查询

```python
from noctalia_i18n_core import WebClient

with WebClient("noctalia", timeout=30) as client:
    project = client.project()
    translations = client.translations(
        "bar.annotate.capture-new-region", locales=("en", "zh-Hans")
    )
    print(project.name, project.progress)
    print(translations)
```

`WebClient` 使用网站 URL 中的项目标识，例如 `noctalia` 或 `official-plugins`。查询返回带类型的不可变对象，译文映射采用 `{key: {locale: value}}` 形式，保留空字符串。

| 方法 | 用途 |
| --- | --- |
| `project()` | 项目信息与各语言进度 |
| `keys(prefix=None)` | 翻译键，可限定点分层级 |
| `catalog(locale=None)` | 当前译文导出，可限定一种语言 |
| `translations(key, *keys, locales=None)` | 精确键的当前译文 |
| `search(query, search_locale=..., locales=None)` | 搜索键名与指定语言的文本 |
| `missing(locale, query=None)` | 缺失项及可用的英文原文 |
| `reviews(locale, query=None)` | 需要复核的译文 |
| `changes(key, *keys, locale=...)` | 指定键和语言的完整网站历史 |
| `iter_changes(...)` | 按语言、时间和修订标识惰性读取项目变更 |
| `change_pages(...)` | 规范化变更分页及原始续读标识 |
| `revision_pages(...)` | 保留空字符串和空值的网站原始分页 |

### 历史筛选与分页

```python
from datetime import UTC, datetime

with WebClient("noctalia", timeout=30) as client:
    for change in client.iter_changes(
        locale="zh-Hans",
        since=datetime(2026, 1, 1, tzinfo=UTC),
    ):
        print(change.key, change.action, change.new_value)
```

`since` 和 `until` 为包含端点、带时区的时间边界。`after` 排除指定原始修订及更早记录。`change_pages()` 和 `revision_pages()` 接受 `start_page`、`cursor`，支持中断后继续扫描。页面移动时会按网站标识定位游标；无法找到游标时抛出 `RuntimeError`。原文语言为英文（`en`），其他语言标识精确匹配。

### HTTP 会话、超时与代理

`timeout` 默认 30 秒，控制每次尝试的连接和读取超时。默认创建的会话对临时网络故障、限流和服务端错误进行三次退避重试。`proxy` 可指定代理，`proxy=""` 表示直连，省略时沿用环境设置。需要自行管理认证、代理或重试策略时传入 `session`，由调用方关闭该会话。

```python
import requests

with requests.Session() as session:
    session.trust_env = False
    with WebClient("noctalia", timeout=30, session=session) as client:
        print(client.project().name)
```

## 数据服务查询

```python
from noctalia_i18n_core import DataClient

with DataClient("noctalia", url="https://i18n.obelus.cc") as client:
    print(client.status())
    changes = client.history(locale="zh-Hans", action="modified", limit=25)
    print(client.translations("bar.annotate.capture-new-region"))
```

`DataClient` 连接本机或远程 HTTP(S) 数据服务，按查询返回已采集的数据。服务地址与项目分别指定，例如 `http://127.0.0.1:8088` 和 `noctalia`。采集、数据库存储及 HTTP 服务由 Noctalia i18n Server 管理。

支持 `project()`、`keys()`、`catalog(locale=None)`、`translations()`、`search()`、`missing()`、`reviews()`、`revision()`、`changes()` 和 `contributors()`。`history()` 按语言、时间、贡献者、动作和精确键筛选，每页最多 1000 条。使用 `before=上一页最后一条记录的 id` 可在新增记录期间稳定向前翻页。

### 增量读取

```python
with DataClient("noctalia", url="http://127.0.0.1:8088") as client:
    cursor = client.updates().cursor
    batch = client.updates(cursor)
    for change in batch.changes:
        print(change.key, change.new_value)
    cursor = batch.cursor
```

`updates()` 返回当前处理位置。传入已保存的游标后，变更按从旧到新排序，返回游标与该批原始记录范围一致。`has_more` 为真时继续读取，处理成功后保存返回游标。没有文本变化的记录也会推进游标，历史回填保留在已有处理位置之前。游标的持久化与应用操作由调用方负责。

客户端按端点保留最近一次响应，通过 ETag 条件请求校验。资源在每个请求中读取一致的已提交视图，跨请求的数据可能更新。`session` 可配置连接策略，外部传入的会话由调用方关闭。

## 返回对象

| 类型 | 契约 |
| --- | --- |
| `Project`, `Locale` | 项目信息及与网站一致的整数完成百分比 |
| `Missing`, `Review` | 条目内容、语言和网站编辑地址 |
| `Revision` | 原始标识、编辑者、前后值和 UTC 时间 |
| `Change` | 新增、修改或删除，以及可用的英文原文上下文 |
| `RevisionPage`, `ChangePage` | 页码、总页数、有序记录和续读标识 |
| `ChangeBatch` | 一致的变更批次、处理游标及后续数据标志 |
| `CollectionStatus` | 已发布的历史范围、目录大小、采集时间和待发布工作 |

原始 `Revision` 区分空字符串和空值。规范化的 `Change` 使用 `None` 表示缺失的前后文本，以 `API` 表示未署名编辑。数据服务历史查询会从已采集的译文和修订补全可用的英文上下文，原文删除保留旧英文文本。返回时间均为带时区的 UTC 时间。

## 异常与资源管理

无效查询参数抛出 `ValueError`。请求失败、返回数据不符合模型或数据尚未采集时抛出带查询上下文的 `RuntimeError`。使用上下文管理器关闭客户端。

接口参数、返回值和异常参见 [`WebClient`](noctalia_i18n_core/web.py)、[`DataClient`](noctalia_i18n_core/data.py) 和[返回对象](noctalia_i18n_core/models.py)。

## 开发

```bash
uv sync --locked
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked pyrefly check
uv run --locked python -m unittest discover -v
```

在线网站检查通过 `uv run --locked python -m unittest tests.live -v` 显式执行。

## 许可证

[MIT](LICENSE)
