# incremental-class-diagram-generator

一个 Claude Code Skill：从真实 Git 变更出发，生成**可编辑、分区化的 draw.io 增量类图**，并把代码问题与设计建议输出到**独立审查报告**。

它解决的是方案设计前的事实问题——先把「改了什么、类之间怎么连、哪里有问题」变成可追溯的结构证据，再谈方案。类图只承载结构事实，代码评审结论不叠加到图上。

## 适用场景

- 整体方案设计前的代码分析
- AI 生成代码的验收
- 合并前评审、重构分析
- 为 [`aibid-approach-diagrams`](../aibid-approach-diagrams) 准备可追溯的类级事实

不用于直接绘制通用业务流程图。

## 安装

把整个目录复制到 Claude Code 的 skills 目录：

```bash
git clone https://github.com/mycn6/incremental-class-diagram-generator.git \
  ~/.claude/skills/incremental-class-diagram-generator
```

重启 Claude Code 后生效。技能以独立副本方式加载，**修改仓库后需要重新同步到 `~/.claude/skills/` 才生效**。

## 使用

在目标仓库中对 Claude 说：

> 用 incremental-class-diagram-generator 分析 HEAD~1..HEAD 的变更，生成增量类图并做代码审查。

或直接指定范围：

> 分析我工作区未提交的改动，输出类图和审查报告。

技能会先执行 `scripts/diagram_gen.py` 提取语义化初稿，再回源码复核成员与关系，然后依次跑样式器与几何优化器，最后校验交付。

## 仓库结构

| 路径 | 职责 |
| --- | --- |
| `SKILL.md` | 核心工作流与到详细参考的链接 |
| `AGENTS.md` | 仓库规则：结构、变更顺序、图契约、工程纪律 |
| `references/` | 类图 XML 契约、视觉约定、代码质量评审标准 |
| `scripts/diagram_gen.py` | 从 Git 范围保守提取代码事实，产出语义化 draw.io 对象 |
| `scripts/style_drawio.py` | 只依据显式元数据施加确定性视觉样式 |
| `scripts/layout_drawio.py` | 确定性分层排布、正交布线、启发式几何检查 |
| `scripts/text_layout.py` | 文字渲染尺寸的唯一事实来源（单向模型，永不低估） |
| `scripts/validate_drawio.py` | 校验结构契约与实测文字是否装得进节点几何 |
| `scripts/test_validate_drawio.py` | 生成、样式、布局、校验的内存回归测试 |
| `assets/class-diagram-example.drawio` | 可编辑的契约示例，不是真实项目的证据 |
| `agents/openai.yaml` | 与 `SKILL.md` 保持一致的 agent 元数据 |

## 环境要求

- Python 3.10+，**零第三方依赖**
- 校验目标仓库需要可用的 `git`

## 校验

在仓库根目录运行：

```bash
python -B scripts/test_validate_drawio.py
python -B scripts/validate_drawio.py assets/class-diagram-example.drawio
python -B scripts/layout_drawio.py --check assets/class-diagram-example.drawio
```

三项分别覆盖回归测试、XML 结构契约与文字装载、几何冲突。

注意：**这些检查只能证明契约合规**。源码真实性、评审判断、业务含义和实际渲染布局都需要另行人工复核。文字装载是基于字宽的估算，残余风险是字体替换。
