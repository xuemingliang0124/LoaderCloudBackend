"""LLM 编排接入（P3，roadmap 核心）：RAG 检索 + Function Calling 工具编排。

子模块（Stage 1~5 按阶段填充）：
- client.py：LLM 调用层 + AnswerOutput 输出模型 + PydanticOutputParser + fallback（FR-07/09）
- tools.py：8 个 @tool 工具 + create_retriever_tool（FR-06）
- orchestrator.py：Retriever + Promptor + Chain 编排（FR-05/06/07）
- guardrail.py：指标校验 ±5% 容差（FR-08）
- report_generator.py：L4 测试报告自动生成

设计约束（SRS 1.5 + HLD A.2）：
- 仅 CPU 可运行：未配置 LLM/Embedding API Key 时走 FakeListChatModel/FakeEmbeddings 降级
- 不可联网微调：仅 Prompt Engineering + Function Calling
- 基于 LangChain 抽象：Loader/Splitter/Embeddings/VectorStore/Retriever/Prompt/Tool/ChatModel/OutputParser
- 业务层禁止 import 具体 SDK（沿用 D2 抽象层约定）
"""
