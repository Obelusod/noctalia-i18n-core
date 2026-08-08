# Noctalia i18n Core

[English](https://github.com/Obelusod/noctalia-i18n-core/blob/main/README.md)

Noctalia i18n Core 是一个只读 Python 客户端，用于读取 [Noctalia Translate](https://i18n.noctalia.dev/) 的项目进度、当前译文和变更记录。存储、调度、投递和应用策略由调用方负责。

## 核心能力

- 读取项目信息、翻译上下文以及项目和各 locale 的翻译进度。
- 以完整目录、精确键结果或文本搜索结果读取当前译文。
- 查询指定 locale 的缺失键和待复核译文。
- 列出点分隔的翻译键并按层级前缀筛选。
- 读取指定 locale 中一个或多个键的完整变更记录。
- 按 locale、时间和锚点边界惰性读取项目级变更流。
- 在应用需要配置连接策略时使用调用方管理的 `requests.Session`。

## 项目结构

```text
noctalia_i18n_core/
├── client.py   # 网站请求与响应验证
└── models.py   # 规范化的项目与变更值
```

## 安装

```bash
uv add noctalia-i18n-core
```

或使用 pip：

```bash
pip install noctalia-i18n-core
```

## 使用指南

### 创建客户端

```python
from noctalia_i18n_core import NoctaliaClient

with NoctaliaClient("noctalia", timeout=30) as client:
    key = "settings.widgets.settings.scroll-repeat.label"
    value = client.translations(key, locales=("zh-Hans",))[key]["zh-Hans"]
    changes = client.changes(key, locale="zh-Hans")

    print(value)
    print(changes[0].occurred_at if changes else "暂无变更")
```

客户端只关闭由自身创建的 session。需要由应用管理代理或重试等连接设置时，应传入已配置的 `requests.Session`。

### 项目信息与翻译进度

`project()` 执行一次项目请求，返回网站公开的项目名称、说明、翻译上下文和当前进度。项目与 locale 的完成度均为网站显示的整数百分比；待复核数表示英文原文在对应译文最后保存后发生过变更的条目数。

```python
project = client.project()

print(project.name, project.key_count, project.language_count, project.progress)
for locale in project.locales:
    print(
        locale.code,
        locale.progress,
        locale.missing_count,
        locale.review_count,
    )
```

### 当前译文

`translations()` 在一次批量请求中读取一个或多个精确键。结果采用键优先结构，并保持请求的键与 locale 顺序。即使所选 locale 均无译文，请求的键仍会保留并对应空映射。

```python
values = client.translations(
    "settings.widgets.settings.scroll-repeat.description",
    "settings.widgets.settings.scroll-repeat.label",
    locales=("zh-Hans", "en"),
)
```

`catalog()` 执行一次完整导出请求并返回 `{key: {locale: value}}`。只有需要全部当前译文时才使用它。

```python
catalog = client.catalog()
source_text = catalog[
    "settings.widgets.settings.scroll-repeat.description"
]["en"]
```

### 文本搜索

`search_translations()` 在 `search_locale` 中搜索键和值，遍历全部结果页，再读取匹配键所需的 locale。如果省略 `locales`，结果只包含 `search_locale`。

```python
matches = client.search_translations(
    "确认",
    search_locale="zh-Hans",
    locales=("zh-Hans", "en"),
)
```

### 缺失与待复核译文

`missing_keys()` 返回指定 locale 中没有当前译文的键。`reviews()` 返回英文原文在该译文最后保存后发生过变化的条目；每条结果同时包含变化前后的英文原文。两种查询均可使用 `query` 筛选结果。

```python
missing = client.missing_keys("fr", query="scroll")
reviews = client.reviews("fr")

for review in reviews:
    print(review.key, review.old_source, review.new_source)
```

### 翻译键

`keys()` 执行一次键树请求并返回完整翻译键列表。`prefix` 匹配以点分隔的完整层级，而非任意字符串前缀。

```python
widget_keys = client.keys(prefix="settings.widgets")
```

### 指定键的变更

`changes()` 读取一个或多个精确键在指定 locale 中的完整变更记录。返回元组按最新到最旧排列；多个键的结果也按该顺序合并。

```python
changes = client.changes(
    "settings.widgets.settings.scroll-repeat.description",
    locale="zh-Hans",
)
for change in changes:
    print(change.occurred_at, change.action, change.new_value)
```

### 项目变更流

`iter_changes()` 按网站提供的顺序惰性读取项目级变更流。`since` 和 `until` 是包含边界的带时区时间，并会统一转换为 UTC；`locale` 将结果限制为一种 locale。

```python
from datetime import UTC, datetime

recent = client.iter_changes(
    locale="zh-Hans",
    since=datetime(2026, 8, 1, tzinfo=UTC),
)
for change in recent:
    print(change.key, change.action)
```

`after` 是网站变更标识。客户端会在产生结果前确认锚点；锚点不存在时抛出 `RuntimeError`。每次请求完成后，`on_progress` 接收已完成页码和当前总页数。

## API 参考

### `NoctaliaClient`

```python
NoctaliaClient(project, *, timeout, session=None)
```

`project` 是网站项目标识。`timeout` 是正的有限秒数。`session` 可以是调用方管理的 `requests.Session`；只有客户端自行创建 session 时才会负责关闭它。

| 成员 | 返回值 |
| --- | --- |
| `slug` | 构造函数接收的项目标识 |
| `project()` | 项目信息与当前翻译进度 `Project` |
| `keys(*, prefix=None)` | 完整或按前缀筛选的键 `tuple[str, ...]` |
| `catalog()` | 完整当前目录 `dict[str, dict[str, str]]` |
| `translations(key, *keys, locales=None)` | 键优先的精确译文 |
| `search_translations(query, *, search_locale, locales=None)` | 文本匹配键的键优先译文 |
| `missing_keys(locale, *, query=None)` | 指定 locale 的缺失翻译键 |
| `reviews(locale, *, query=None)` | 指定 locale 的待复核译文 |
| `changes(key, *keys, locale)` | 按最新到最旧排列的精确键变更 |
| `iter_changes(*, locale=None, since=None, until=None, after=None, on_progress=None)` | 惰性的项目级变更迭代器 |
| `close()` | 关闭由客户端拥有的 HTTP session |

`translations()` 和 `changes()` 至少需要一个位置键参数。`translations()` 即使结果为空也会返回请求的键。`changes()` 返回元组，并为每个请求键访问网站。`iter_changes()` 不接受键参数，只有开始迭代时才会发起请求。

无效参数抛出 `ValueError`；网络故障以及不符合网站契约的响应抛出 `RuntimeError`。

### `Project` 与 `Locale`

`Project` 包含项目的公开信息和由当前 locale 状态汇总的进度：

| 字段或属性 | 说明 |
| --- | --- |
| `slug` | 网站项目标识 |
| `name` | 项目名称 |
| `description` | 项目说明；未设置时为 `None` |
| `translation_context` | 翻译说明与术语约定；未设置时为 `None` |
| `key_count` | 当前翻译键数 |
| `locales` | 按网站顺序排列的 `tuple[Locale, ...]` |
| `language_count` | 已配置 locale 数 |
| `translation_count` | 当前已有译文总数 |
| `missing_count` | 当前缺失译文总数 |
| `review_count` | 当前待复核译文总数 |
| `progress` | 与网站一致的整数完成百分比 |

每个 `Locale` 包含 `code`、`translation_count`、`missing_count`、`review_count` 和派生的 `progress`。尚无任何译文的 locale 也会包含在项目结果中。

### `Review`

每条待复核译文包含 `key`、`locale`、`old_source`、`new_source` 和 `translation_url`；不存在旧原文时，`old_source` 为 `None`。原文差异来自网站的 Needs review 查询，不代表译文自身的变更记录。

### `Change`

每条变更记录包含以下字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 网站变更标识 |
| `key` | 翻译键 |
| `locale` | 发生变更的 locale |
| `action` | `added`、`modified` 或 `deleted` |
| `old_value` | 变更前文案；新增时为 `None` |
| `new_value` | 变更后文案；删除时为 `None` |
| `occurred_at` | 带 UTC 时区的 `datetime` |
| `translation_url` | 对应键和 locale 的翻译页面 |
| `actor_login` | 贡献者登录名；自动变更为 `None` |
| `actor_url` | 可用时的贡献者资料链接 |
| `actor_avatar_url` | 可用时的贡献者头像链接 |

`ChangeAction` 是字面量类型 `"added" | "modified" | "deleted"`。`SOURCE_LOCALE` 的值为 `"en"`。

## 性能参考

以下数据来自 2026 年 8 月公开的 `noctalia` 项目，描述的是网络请求，不包含本地缓存。

| 操作 | 请求 | 参考结果 |
| --- | --- | --- |
| `project()` | 一次项目页面请求 | 25 个 locale 的项目进度，约 23 KB |
| `catalog()` | 一次完整导出 | 42,857 条译文，约 2.23 MB，约 4.4–4.8 秒 |
| `translations()` | 一次批量请求 | 两个键、两个 locale，约 1.3 KB，约 1.4 秒 |
| `search_translations()` | 搜索页面及一次批量请求 | 简体中文匹配 19 个，约 1.7 秒 |
| `missing_keys()` | 一次缺失查询 | 返回指定 locale 的全部匹配键 |
| `reviews()` | 一次待复核查询 | 返回匹配键及其英文原文差异 |
| `keys()` | 一次键树请求 | 约 2,474 个键，147 KB |
| `changes()` | 每个请求键一次请求 | 示例键约 1.3 KB，约 1.4 秒 |
| `iter_changes()` | 读取变更页直到边界 | 取决于请求范围 |

实际耗时受网络延迟、服务器负载和项目规模影响。精确查询和文本搜索不会下载完整当前目录。

## 开发

安装锁定的开发环境：

```bash
uv sync --locked
```

运行格式化、代码检查、类型检查和测试：

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run python -m unittest discover -v
```

显式运行在线契约测试，核验所有公开操作与 Noctalia Translate 当前公开项目的实际响应：

```bash
uv run python -m unittest tests.live -v
```

构建并检查 wheel 和源码包：

```bash
uv build --no-sources --clear
uvx twine check --strict dist/*
```

## 许可证

[MIT](LICENSE)
