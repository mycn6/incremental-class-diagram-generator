# 仓库规则

## 范围与结构

- `SKILL.md` 只承载核心工作流，以及指向详细参考的直接链接。
- `references/` 承载事实清单契约、类图导出 XML 契约、视觉约定与评审标准。
- `scripts/diagram_gen.py` 从 Git 变更提取保守的代码事实，写出事实清单；`.drawio` 导出是清单的纯函数，按需生成。
- `scripts/facts_io.py` 定义事实清单的模式，负责序列化、按 `origin` 合并与结构校验。它不扫描源码，也不度量文字。
- `scripts/normalize_drawio.py` 把一份 `.drawio` 的行尾归一化为 LF。它先校验严格 UTF-8 与 XML 良构，再只替换 CRLF——除此之外一个字节都不动。它是把 Windows 上行尾的 `.drawio` 重新收进仓库的**唯一**通道（见「工程纪律」）。
- `scripts/style_drawio.py` 只依据显式元数据施加确定性视觉样式。
- `scripts/layout_drawio.py` 执行确定性分层排布、显式正交布线，以及启发式 XML 几何检查。
- `scripts/text_layout.py` 以单向（永不低估）的模型估算文字的渲染尺寸。它是节点尺寸的唯一事实来源：生成器与校验器必须共用它，否则两者对「文字是否装得下」的判断会分叉。任何其他模块都不得重复实现这套度量。
- `scripts/validate_drawio.py` 检查结构契约，以及实测文字是否装在其节点几何内；它不得声称语义或视觉正确。
- `scripts/test_validate_drawio.py` 承载生成、样式、布局、校验的内存回归测试。
- `assets/class-facts-example.json` 与 `assets/class-diagram-example.drawio` 分别是清单与导出两侧的可编辑契约示例，都不是真实项目的证据。
- `agents/openai.yaml` 必须与 `SKILL.md` 保持一致。

## 变更顺序

1. 仓库规则变化时，先改本文件。
2. 改变行为前，先改 `SKILL.md` 或相关参考文档。
3. 再改脚本，实现已文档化的契约。
4. 用同一份元数据更新可编辑示例与测试。
5. 交付前跑完全部校验命令。

## 图契约

本节约束 `.drawio` 导出物。交付物是事实清单，图是它的可选导出。

- 保持 draw.io XML 不压缩：`mxfile > diagram > mxGraphModel > root`。
- 两个视觉维度相互独立：UML 语义与 Git 变更状态。代码评审结论不属于类图 XML。
- 每个类都是可编辑的矩形分类器容器，其标题与语义分区是它的子对象。不得用一个 label 内的分隔符字符去模拟分区。
- 按需使用 `role=class-header`、`role=class-attributes`、`role=class-operations`、`role=class-literals`、`role=class-separator`。关系连接到外层 `role=class` 容器。
- 节点宽度与分区高度由实测文字推导，绝不硬编码。生成的 label 必须在生成期硬折行，使其逻辑行数等于渲染行数；分区高度必须随内容增长。生成器必须在布局运行前定好框的尺寸，因为关系端口以分数存储（会随尺寸调整而移动），而拐点是绝对坐标（不会）。
- 行预算内装不下其成员的分区必须丢弃成员，并在末尾用一行 `… 另有 N 项` 声明丢弃了多少，该行本身也计入这份预算。绝不静默截断，也绝不让可见数目与实际省略的成员数不一致。
- 接口显示 `«interface»`；抽象类显示 `«abstract»` 且标题斜体；枚举显示 `«enumeration»` 并带 literals 分区；结构体显示 `«struct»`。
- 逻辑错误、校验盲区、测试缺口、设计问题、模式分析、优化点、待验证项，保留在独立的评审报告与下游交接中，使用中文类别名并附源码证据。
- 每一页必须包含一个可编辑的 `role=legend` 容器，为每种受支持的分区式 UML 类型、关系箭头与 Git 变更状态提供原生子图形。纯文字图例无效。
- 图例子项使用显式的 `role=legend-item`、`role=legend-item-section`、`role=legend-edge`、`role=legend-text`，以及稳定的 `legend_group` 与 `legend_key` 元数据。它们必须在视觉上与真实图对象所用样式一致。
- 不要在增量类图上加重要度或难度徽标。整体方案的重要度与难度属于下游 `aibid-approach-diagrams` 技能。
- 保持源码与关系证据机器可读。证据不足的关系直接省略，把不确定性记入评审报告，不要凭空造边。
- 解析关系目标时先按声明的完整类型名匹配，再退回短名。短名被本页多个类共用时**一律不连线**，并把该条关系报告到 stderr——不得在多个同名类之间猜测，猜错会画出一条与它自己的 evidence 字符串相矛盾的边，复核者看不出来；不画只是缺一条边，复核者会发现。目标不在此页是既定默认（框架、标准库与间接类型本就排除），静默跳过，不报告。
- 图内对象只承载图的语义。工具故障与歧义报告走 stderr，不得写入图内任何对象；`role=note` 的范围说明只描述这张图覆盖了哪些维度，不报告本次扫描的运行状态。
- 对理解直接关系所必需的未变类型，使用机器可读的 `change=unchanged` 与中性灰状态样式。直接显示类名，不加可见的 `[未变]` 前缀。生成器不得自动添加它们；执行 agent 最多选取一个关系距离内、有证据支撑的未变类型，且不得为此暂停征求用户输入。标准库、框架、工具类、异常、常规 DTO 与间接类型默认排除。
- 样式脚本只可改变分类器、分区、关系与 Git 状态的样式。它必须保留已写好的坐标、路由、label、证据与无关对象。
- 文字使用单一支持 CJK 的字体族，由 `scripts/style_drawio.py` 中的 `TEXT_FONT` 定义。任何可见 label 含 CJK 字符的单元格都必须声明包含 CJK 字体族的 `fontFamily`。样式脚本不得把文字切到不含 CJK 的字体族，也不得用仅含拉丁字符的字体覆盖已写好的字体。
- 布局脚本可以移动类节点、为关系边布线，以及在优化后的类区域变大时下移那些在优化前就已位于类区域下方的顶层支撑对象。它必须保留 label、证据、语义元数据与嵌套的已写好对象。
- 布局脚本必须使用显式端口与 `mxPoint` 拐点，检查线-节点交叉、线-线交叉与共线路由重叠，然后报告任何未解决的几何冲突。几何检查是启发式的，不得声称渲染后的视觉正确性。
- 校验器只能证明契约合规。源码真值、业务含义、评审判断与渲染布局都需要另行评审。

### 已知限制：外围通道的竖折线不分配 x 车道

`layout_drawio.py` 的 `outer_indices` 按通道名（`top`/`bottom`/`gutter`）计数，车道偏移只加在横线的 y 上；gutter 的竖线 x 直接由某一端的框边界推导（`source_rect.right + GUTTER` 之类），完全不含车道序号。因此**同一个 gutter 通道内**的两条边，只要相关的那一端框边界相同，竖线就落在同一个 x 上，在重叠的 y 区间里叠住。

触发条件比「同一目标同一侧」更宽：竖线 x 只由框边界决定，所以**框同列**就撞。2026-09-15 用 8 类 / 9 关系的内存夹具实测，共出现三处共用 x——`class-5` 与 `class-8` 两个**不同**目标共用 `x=912`（二者同列），`class-3` 被两条边共用 `x=462`（同一目标同一侧），三条边共用 `x=368`（源端同列）。原描述里的「分属不同通道名」与代码不符：撞 x 的边全在同一个 `gutter` 通道内。

这是 2026-09-15 首次对真实仓库跑端到端时发现的**既有缺陷**，与「主产物改为事实清单」那次重构无关：`--base HEAD --head WORKTREE` 的纯扫描导出即报 1 条，补上复核关系后增至 6 条。小夹具盖不住它（`assets/class-diagram-example.drawio` 只有 2 条边），当时 105 个测试全绿也不能说明它不存在。布局器重写模式加 `--max-iterations 40` 仍报同一条，说明这不是迭代次数问题。

**因此：`validate_drawio.py` 是导出的硬关口，`layout_drawio.py --check` 是尽力而为的几何检查。** 残余 `edge-crossing` 按如实记录处理——写进报告的「事实清单与类图」项，让复核者知道这份导出在密集汇入处存在折线重叠。不得手工改导出物的拐点或端点来消掉它（下次导出会覆盖），不得为了让它通过而删除关系。要真正修复必须改 `layout_drawio.py` 的车道分配；判据是下面那个回归用例，它现在是预期失败。

**已试过、不要重试的三种局部修法**（2026-09-15 在同一夹具上实测，基线 12 条 `edge-crossing`）：只给 gutter 竖线加车道偏移仍报 12 条；改按几何跨度排序分配车道报 13 条；改按端口 y 排序报 15 条。几何确实变了（竖线 x 从 368 变为 420），但一条也没消掉——因为给竖线分配不同的 x 会引入「端口 → 车道 x」的横移段，反而与别的竖线交叉，此消彼长。这是标准的通道布线问题，车道分配要同时满足三个约束：同端同列的两条边竖线不共线、竖线下落不穿内侧车道的横线、端口到车道的横移段不穿别的竖线。而且源侧与目标侧对车道顺序的要求可能互相矛盾，无解处需要三段式狗腿。修它是一轮独立工作，不是补丁。

复现夹具见 `scripts/test_validate_drawio.py` 的 `test_two_edges_entering_one_target_share_a_gutter_lane`：8 类 / 9 关系，纯内存构造，当前被标为 `expectedFailure`。真正修好车道分配后，去掉那个装饰器即可验证。

### 已修复：导出物的行尾曾随平台变化

2026-09-15 之前，三个 drawio 序列化点——`diagram_gen.write_document`、`layout_drawio`、`style_drawio`——都用 `ET.ElementTree(tree).write(path, encoding="utf-8", xml_declaration=True)`。CPython 在参数是**文件名**时走文本模式，`\n` 按 `os.linesep` 落盘，于是 Windows 产出 CRLF、Linux/macOS 产出 LF，跨平台的逐字节确定性不成立。当时 `assets/class-diagram-example.drawio` 的 368 个 CR 就是这个缺陷的产物。

现已三处都改为先开二进制句柄再写（见「工程纪律」），夹具一并归一化为 LF。这条记录留着，是因为它解释了两件事：为什么夹具曾经是 CRLF，以及为什么 `.gitattributes` 的 `-text` 单独并不足以保证字节级契约——`-text` 只保证检出不转换，管不了脚本写出什么。

## 事实清单契约

- 事实清单（JSON）是唯一事实来源与交付物；`.drawio` 是它的纯函数导出，按需生成。
- 一次 Git 范围对应一份清单文件，不跨范围累积。范围标识进文件名，使同一范围可被反复重扫并合并。
- 每个类与每条关系都带 `origin`：`scan` 由脚本扫描得到，重扫时整体替换；`agent` 由复核判断得到，重扫时保留。目标清单已存在且范围不同时必须拒绝覆盖并索要显式 `--force`，不得静默合并无关范围。
- 类记录携带 `id`、`name`、`kind`、`change`、`source`、`origin`、`fields`、`methods`；关系记录携带两端、`to_declared`、`kind`、`evidence`、`origin`。
- 每条关系必须有非空 `evidence`，且必须与边指向的类讲同一件事。猜错画出的边与自己的证据矛盾而复核者看不出来；不画只是缺一条边，复核者会发现。
- 目标不在本页的关系保留记录，`to` 为 `null` 并保留 `to_declared`。这是既定默认（框架、标准库与间接类型本就排除），不是缺陷。
- 成员列表在清单中无损。导出受分区行预算约束、会丢弃成员并声明 `… 另有 N 项`；两者不符时以清单为准。
- `scan.warnings` 是本次扫描运行状态的合法去处：跳过的文件、读不了的文件、未连线的歧义关系。图内对象仍不得承载运行状态——本节约束清单，图契约约束图，两者不冲突。
- 硬失败只给结构性的错误：`id` 重复、关系端点指向不存在的 `id`、`kind` 不在受支持集合、`evidence` 为空、`source` 为空或逃出仓库、`change` 不在四种状态。其余情况（例如证据串里没出现目标短名）只告警——证据可以合法地通过变量指代目标，校验器太严会把 agent 推回手写 XML。

## 工程纪律

- 编辑使用 `apply_patch`。
- `.drawio` 文件只通过编辑器/文件工具或本仓库脚本创建与改写。不得通过 shell 重定向、内联 `-c` 脚本，或任何不保证 UTF-8 的通道写入。行尾不合规的 `.drawio`（例如在 Windows 上手工编辑过）用 `scripts/normalize_drawio.py` 收回来，不要用 `tr`、`sed` 或重定向——那条禁令没有任何例外。
- 写出 `.drawio` 必须先把文件以二进制模式打开，再交给 `ET` 写：`with open(output, "wb") as handle: tree.write(handle, encoding="utf-8", xml_declaration=True)`。直接把路径传给 `ET.ElementTree.write` 会走文本模式，`\n` 被按 `os.linesep` 翻译，Windows 上写出 CRLF、Linux/macOS 上写出 LF，跨平台的逐字节确定性随之失效。导出的行尾必须恒为 LF。
- 事实清单同样是 agent 可编辑的产物，必须是严格 UTF-8（中文证据串在其中，理由同上）。脚本重扫时不得丢弃 `origin="agent"` 的条目；目标文件已存在且范围不同时必须拒绝而非覆盖。
- 脚本保持零依赖，兼容 Python 3.10+。
- 测试必须在内存中运行，不得删除用户文件或目录。
- 不得向仓库添加缓存、生成的预览或临时输出。
- 不得修改密钥、环境文件、CI/CD、Git 历史或外部项目。
- `.gitignore` 排除 `__pycache__/`、`*.py[cod]` 与本机的 `.claude/settings.local.json`。前者是上一条的直接落实，后者含本机绝对路径。
- `.gitattributes` 对 `*.drawio` 与 `*.json` 声明 `-text`。本机 `core.autocrlf=true`，不声明就会在检出时把它们改写成 CRLF：`.drawio` 是字节级契约，破坏即失效；事实清单由 `facts_io.dump_facts` 以 LF 写出，检出为 CRLF 会让每次重扫都产生一份整文件 diff，掩盖真正改动的那几行。

## 校验

在仓库根目录用可用的 Python 解释器运行：

1. `python -B scripts/test_validate_drawio.py`
2. `python -B scripts/validate_drawio.py assets/class-diagram-example.drawio`
3. `python -B scripts/layout_drawio.py --check assets/class-diagram-example.drawio`
4. `python -B scripts/facts_io.py --check assets/class-facts-example.json`
5. `python -B scripts/normalize_drawio.py --check assets/class-diagram-example.drawio`
6. `python -B <skill-creator>/scripts/quick_validate.py .`

当 `diagram_gen.py` 或 `layout_drawio.py` 变化时，通过回归测试套件覆盖其内存路径。实际的 draw.io 渲染仍是单独的人工检查；本机无兼容渲染器时必须记为「未执行」。`quick_validate.py` 本机不存在，同样记为未执行。

`scope.generated_at` 是两次相同扫描之间唯一会变动的字段。比较两份清单前先归一化该字段，否则 diff 永远不空。

生效的 skill 是 `~/.claude/skills/incremental-class-diagram-generator` 下的独立副本。改完仓库必须手动同步，用 `diff -r -q`（排除 `.git`、`__pycache__`、`.claude`）确认一致，再在副本目录里重跑上面 1–5——在仓库里绿不能证明副本是绿的。
