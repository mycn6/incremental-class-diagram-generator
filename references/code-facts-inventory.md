# 事实清单与关系证据契约

## 目录

- [定位](#定位)
- [字段](#字段)
- [按来源合并](#按来源合并)
- [三种关系状态](#三种关系状态)
- [证据怎么写](#证据怎么写)
- [无损与截断](#无损与截断)
- [清单里不得放什么](#清单里不得放什么)

## 定位

事实清单是一次 Git 范围内**可核对的代码事实**：改了哪些类型、有哪些成员、之间有哪几条有证据的关系、每条事实从哪来。它是审查报告的依据，也是本 Skill 的交付物。类图是它的可选导出，导出物的契约见 [类图导出与 XML 契约](drawing-conventions.md)。

清单不承载审查结论。评分、严重度、问题分类、模式分析、优化建议全部留在报告里——判读需要人，事实不需要。

一次范围一份文件，不跨范围累积：

```
docs/code-review/<范围标识>-facts.json
```

`--base X --head Y` 的范围标识是稳定的（如 `head-worktree-facts.json`），同一范围可以反复重扫并合并。`--days` 与 `--files` 不是稳定的范围标识——今天的 `--days 7` 和明天的不指同一批提交——文件名前置日期，避免隔天把两个不相干的范围合进一份。

## 字段

```json
{
  "schema": 1,
  "scope": {
    "label": "HEAD..WORKTREE",
    "base": "HEAD",
    "head": "WORKTREE",
    "generated_at": "2026-09-15T14:02:11+08:00"
  },
  "classes": [
    {
      "id": "OrderService",
      "name": "OrderService",
      "kind": "class",
      "change": "added",
      "source": "src/order.py",
      "origin": "scan",
      "fields": ["- repository: OrderRepository", "- clock: Clock"],
      "methods": ["+ create(command: CreateOrder): Order",
                  "+ cancel(order_id: str): None"]
    }
  ],
  "relations": [
    { "from": "OrderService", "to": "BaseService", "to_declared": "BaseService",
      "kind": "inheritance",
      "evidence": "src/order.py: OrderService declares BaseService",
      "origin": "scan" },
    { "from": "OrderService", "to": null, "to_declared": "User",
      "kind": "inheritance",
      "evidence": "src/order.py: OrderService declares User",
      "origin": "scan" }
  ],
  "files": ["src/order.py", "src/service.py"],
  "warnings": [
    "relation not drawn: src/order.py: OrderService declares User (User is claimed by 2 classes on this page)"
  ]
}
```

`scope.generated_at` 是两次相同扫描之间唯一会变动的字段。比较两份清单前先归一化它。

**类记录**

| 字段 | 说明 |
| --- | --- |
| `id` | 稳定键，关系用 `from`／`to` 引用它。取声明名，重名时派生 `原名#2` |
| `name` | 显示用的完整点分名 |
| `kind` | `class`｜`interface`｜`abstract`｜`enum`｜`struct` |
| `change` | `added`｜`modified`｜`removed`｜`unchanged` |
| `source` | 仓库相对路径 |
| `origin` | `scan`｜`agent` |
| `fields`／`methods` | 逐条成员签名，完整无损 |

**关系记录**（顶层平铺，不嵌在类里——agent 补的关系集中在一处，改起来不必翻遍每个类）

| 字段 | 说明 |
| --- | --- |
| `from`／`to` | 两端的 `id`。`to` 为 `null` 表示目标不在本页 |
| `to_declared` | 源码里声明的原始类型名，始终保留 |
| `kind` | `inheritance`｜`implementation`｜`association`｜`aggregation`｜`composition`｜`dependency` |
| `evidence` | 非空，必须与边指向的类讲同一件事 |
| `origin` | `scan`｜`agent` |

键名未知的额外字段一律原样保留，脚本不删。

## 按来源合并

重扫同一范围时按 `origin` 分流，这是清单能当事实来源的前提：

| 条目 | 重扫时 |
| --- | --- |
| `origin: "scan"` 的类与关系 | 整体替换为新扫描结果——源码才是这些事实的依据 |
| `origin: "agent"` 的类与关系 | **保留**，不动 |
| agent 类的 `source` 已不在本次变更文件里 | 保留 + 告警（可能真的没了，也可能只是这点没改到） |
| agent 关系的任一端点不再能解析 | 保留 + 告警 |
| `warnings` | 重建 |

目标文件已存在且 `scope` 与本次调用不同时，脚本拒绝写入并要求显式 `--force`。不静默合并无关范围，也不静默丢弃 agent 条目——两者都会让清单悄悄失真，而清单失真是没有别的机制能兜住的。

## 三种关系状态

1. **解析成功**：`to` 指向本页一个类。
2. **目标不在本页**：`to` 为 `null`，保留 `to_declared`。框架基类、标准库类型、间接依赖本就排除，这是既定默认，不是缺陷，不告警。
3. **短名被本页多个类共用**：同样 `to` 为 `null`，但记入 `warnings`，并在导出时 WARN 到 stderr。

第 3 种**选"不画"而不是"猜一个"**，因为两者的失败方式不对称：猜错画出一条与自身 `evidence` 矛盾的边，复核者读 `evidence` 时不会发现箭头搭错了；不画只是缺一条边，复核者对着源码补即可。WARN 就是让他知道该去补哪一条。

补的方式是在清单里把 `null` 改成确切的 `id`，不是在导出的 XML 里改。

## 证据怎么写

`evidence` 必须能让人从源码复现这条关系，且必须与边指向的类讲同一件事。

生成器合成的形式是 `<文件>: <类> declares <原始目标名>`——它只证明"源码里写了这个名字"。复核时**应当改写成能说明关系性质的版本**：

```
src/order.py: OrderService.__init__ stores OrderRepository
src/order.py: OrderService.cancel dispatches cancellation to EventBus
src/repository.py: OrderRepository extends BaseRepository<Order>
```

不要因同文件或名称相似就写证据。临时局部变量通常只表示依赖；生命周期由整体控制且对象不能独立存在时才写组合。证据不足的关系**不写进清单**，把不确定性记进报告的待验证事项——不要凭空造边。

脚本会检查证据串里是否出现了 `to_declared` 的短名，**不出现只告警不失败**：证据完全可以合法地通过变量指代目标。

## 无损与截断

清单里的 `fields`／`methods` 是完整的。导出的类图受分区行预算约束，超过的部分会丢弃并保留一行 `… 另有 N 项`。

所以同目录下清单有 40 条方法、图上只显示 12 条是正常现象。**两者不符时以清单为准**，图是给人看层级的，清单是给复核用的。

## 清单里不得放什么

- 审查结论、评分、严重度、整改项——进报告；
- 设计模式建议与优化建议——进报告；
- 整体方案的重点与难点——属于下游 `aibid-approach-diagrams`；
- 扫描器的能力边界说明——进报告的限制章节，不要混进事实。

清单里只有可以被源码证实或证伪的东西。
