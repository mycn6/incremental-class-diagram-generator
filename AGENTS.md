# 仓库规则

## 范围与结构

- `SKILL.md` 只承载核心工作流，以及指向详细参考的直接链接。
- `references/` 承载类图 XML 契约、视觉约定与评审标准。
- `scripts/diagram_gen.py` 提取保守的代码事实，产出语义化的 draw.io 对象。
- `scripts/style_drawio.py` 只依据显式元数据施加确定性视觉样式。
- `scripts/layout_drawio.py` 执行确定性分层排布、显式正交布线，以及启发式 XML 几何检查。
- `scripts/text_layout.py` 以单向（永不低估）的模型估算文字的渲染尺寸。它是节点尺寸的唯一事实来源：生成器与校验器必须共用它，否则两者对「文字是否装得下」的判断会分叉。任何其他模块都不得重复实现这套度量。
- `scripts/validate_drawio.py` 检查结构契约，以及实测文字是否装在其节点几何内；它不得声称语义或视觉正确。
- `scripts/test_validate_drawio.py` 承载生成、样式、布局、校验的内存回归测试。
- `assets/class-diagram-example.drawio` 是可编辑的契约示例，不是真实项目的证据。
- `agents/openai.yaml` 必须与 `SKILL.md` 保持一致。

## 变更顺序

1. 仓库规则变化时，先改本文件。
2. 改变行为前，先改 `SKILL.md` 或相关参考文档。
3. 再改脚本，实现已文档化的契约。
4. 用同一份元数据更新可编辑示例与测试。
5. 交付前跑完全部校验命令。

## 图契约

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

## 工程纪律

- 编辑使用 `apply_patch`。
- `.drawio` 文件只通过编辑器/文件工具或本仓库脚本创建与改写。不得通过 shell 重定向、内联 `-c` 脚本，或任何不保证 UTF-8 的通道写入。
- 脚本保持零依赖，兼容 Python 3.10+。
- 测试必须在内存中运行，不得删除用户文件或目录。
- 不得向仓库添加缓存、生成的预览或临时输出。
- 不得修改密钥、环境文件、CI/CD、Git 历史或外部项目。
- `.gitignore` 排除 `__pycache__/`、`*.py[cod]` 与本机的 `.claude/settings.local.json`。前者是上一条的直接落实，后者含本机绝对路径。
- `.gitattributes` 对 `*.drawio` 声明 `-text`。本机 `core.autocrlf=true`，不声明就会在检出时把 `.drawio` 改写成 CRLF，破坏字节级契约。

## 校验

在仓库根目录用可用的 Python 解释器运行：

1. `python -B scripts/test_validate_drawio.py`
2. `python -B scripts/validate_drawio.py assets/class-diagram-example.drawio`
3. `python -B scripts/layout_drawio.py --check assets/class-diagram-example.drawio`
4. `python -B <skill-creator>/scripts/quick_validate.py .`

当 `diagram_gen.py` 或 `layout_drawio.py` 变化时，通过回归测试套件覆盖其内存路径。实际的 draw.io 渲染仍是单独的人工检查；本机无兼容渲染器时必须记为「未执行」。
