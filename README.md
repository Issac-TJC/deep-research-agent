# Enterprise Deep Research Agent Lab

这个仓库用于学习、设计并逐步实现一个可向企业组织提供服务的 Deep Research Agent。

## 当前内容

- [`docs/deep-research-agent-blueprint.md`](docs/deep-research-agent-blueprint.md)：总体架构、研究流程、企业能力、自定义扩展机制、评测与实施路线。
- `references/hyperresearch/`：Hyperresearch 参考实现（研究方法、证据库、引用验证）。
- `references/OpenResearch/`：OpenResearch 参考实现（多 Agent 工作区、实验树、异构算力、桌面产品）。

## 推荐阅读顺序

1. 先读蓝图的“参考项目拆解”，理解两个项目分别解决什么问题。
2. 再读“目标架构”和“端到端状态机”，建立完整系统视图。
3. 按“从零到企业版的实施路线”实现 MVP，不要从微服务或多 Agent 数量开始。
4. 用“自定义功能设计”中的扩展点增加垂直行业能力。
5. 每个阶段都执行“评测与发布门禁”，避免把演示效果误当成可靠性。

## 当前建议

第一版应采用“模块化单体控制面 + 独立沙箱 Worker + Durable Workflow + PostgreSQL/对象存储”的形态。先跑通一个高价值垂直场景和完整证据链，再按负载与隔离边界拆服务。

