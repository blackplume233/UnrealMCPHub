# 改动分桶与分支策略

## 目标

这份文档用于把当前仓库中的新增内容分成两类：

1. 长期保留在 fork 中的内容
2. 未来适合清洗后向上游发起 PR 的内容

同时给出对应的分支承载方式，避免实验改动、团队私有工作流和通用改进混在一起。

## 推荐远程结构

- `origin`
  你的 fork，长期保存你的实验和团队工作流
- `upstream`
  官方仓库，用于同步原始版本和准备 PR

## 推荐分支职责

### `main`

用途：
- 保持接近上游
- 同步 `upstream/main`
- 作为其他分支的干净基线

不要在这里长期直接做实验。

### `codex/lab`

用途：
- 承载长期试错
- 放本地便捷脚本和探索性改动
- 容纳暂时不适合 PR 的实验内容

适合放：
- Hub 打包版兼容性排查脚本
- 本地 bench 辅助脚本
- 临时运行入口
- 环境探测与恢复类实验

### `codex/team-workflow`

用途：
- 承载团队自己的工作流、规则和研究文档
- 沉淀项目级 domain knowledge

适合放：
- workflow
- rules
- todo
- benchmark 研究和试错报告
- 团队 wrapper skill 设计文档

### `codex/benchmark`

用途：
- 承载 benchmark 相关的任务模板、轻量 benchmark、执行记录结构

适合放：
- benchmark-lite 方案
- benchmark 模板
- 打分记录结构
- 验证清单

### `codex/pr-*`

用途：
- 从 `main` 或相对干净的功能分支切出
- 只包含一组可审查、可合并的通用改动

适合放：
- bugfix
- 文档修正
- 通用 discover 修复
- 明确对其他用户也有价值的改进

## 当前改动分桶

## A. 长期保留在 fork

这些更像你的团队资产，不适合作为第一批上游 PR：

- [workflow.md](C:\Users\alain\Documents\Playground\UnrealMCPHub\docs\unreal-ai-playbook\workflow.md)
- [rules.md](C:\Users\alain\Documents\Playground\UnrealMCPHub\docs\unreal-ai-playbook\rules.md)
- [todo.md](C:\Users\alain\Documents\Playground\UnrealMCPHub\docs\unreal-ai-playbook\todo.md)
- [skill-system.md](C:\Users\alain\Documents\Playground\UnrealMCPHub\docs\unreal-ai-playbook\skill-system.md)
- [benchmark-research-report.zh-CN.md](C:\Users\alain\Documents\Playground\UnrealMCPHub\docs\unreal-ai-playbook\benchmark-research-report.zh-CN.md)
- 你们后续会补的 benchmark-lite、task templates、review checklist

原因：
- 带有明显团队流程属性
- 偏你们自己的研究和试错路径
- 更适合先在 fork 中演化

推荐分支：
- `codex/team-workflow`

## B. 先保留在 lab，后续视情况上浮

这些内容现在有价值，但还不够确定，先不要急着 PR：

- [run-unrealhub-source.bat](C:\Users\alain\Documents\Playground\UnrealMCPHub\tools\run-unrealhub-source.bat)
- [run_unrealhub_source.py](C:\Users\alain\Documents\Playground\UnrealMCPHub\tools\run_unrealhub_source.py)
- 打包版 `discover` 的兼容性观察和临时绕行方案

原因：
- 目前主要是为当前环境服务
- 还缺少跨环境验证
- 可能最终演化成真正的 bugfix 或正式 launcher 脚本

推荐分支：
- `codex/lab`

## C. 未来适合整理成 PR

如果后续验证成熟，这些方向最适合上游化：

- `discover` 的兼容性修复
- 更稳定的 instance identification
- 文档里补充对 Windows 二进制 UE + C++ toolchain 的说明
- benchmark 前置条件和“先跑 Lite 再跑正式 benchmark”的建议
- `.gitignore` 或开发者文档里的轻量工程卫生改进

推荐分支：
- 从 `main` 切 `codex/pr-discover-fix`
- 从 `main` 切 `codex/pr-docs-benchmark-setup`

## 当前提交策略

### 第一组提交

分支：
- `codex/team-workflow`

内容：
- `docs/unreal-ai-playbook/` 下的团队工作流文档
- 当前这份分桶与分支策略文档
- 必要的 `.gitignore` 调整

目标：
- 先把团队资产整理成一组干净、稳定、低风险的提交

### 第二组提交

分支：
- `codex/lab`

内容：
- 轻量 wrapper 脚本
- 本地运行辅助脚本

目标：
- 保留当前可工作的本地实验链路
- 暂不把它伪装成可上游的正式功能

## 同步建议

建议的常规节奏：

1. `main` 同步 `upstream/main`
2. 把新的 `main` 合并到 `codex/team-workflow`
3. 把新的 `main` 合并到 `codex/lab`
4. 当某个实验足够干净，再从 `main` 切 `codex/pr-*`

## 判断标准

如果一个改动符合下面大多数条件，就更适合发 PR：

- 与团队私有流程无关
- 对其他使用者普遍有价值
- 不依赖本地特殊环境
- 可被清楚解释和验证
- 改动边界小

如果不符合，就先留在 fork 里继续长。
