# Architecture index

本目录保存 service-sdk 的可校准架构基线及跨产品 Realtime Provider Program 的权威边界。

- [`ARCHITECTURE.md`](ARCHITECTURE.md)：当前代码事实、目标 Realtime 分层、典型调用链、跨仓职责、兼容与发布不变量。
- [`protocols/qwen-native-2026-08-28.1/manifest.json`](protocols/qwen-native-2026-08-28.1/manifest.json)：Qwen3.5 Omni
  Realtime 原生 wire authority、跨语言 vectors 与 SHA-256 清单。
- [`protocols/openai-native-2026-08-27.1/manifest.json`](protocols/openai-native-2026-08-27.1/manifest.json)：本轮 OpenAI
  Realtime offline adapter 的 wire authority；未来 live enablement 的真实凭证、付费与生产路由保持关闭。
- [`protocols/tokenseller-realtime-control-2026-08-28.1/manifest.json`](protocols/tokenseller-realtime-control-2026-08-28.1/manifest.json)：
  TokenSeller mint、capability negotiation、控制错误与受控关闭的跨 Python/TypeScript authority。
- [`protocols/realtime-local-2026-08-27.1/manifest.json`](protocols/realtime-local-2026-08-27.1/manifest.json)：AIPhone AF_UNIX
  与 Simple Harness loopback WSS 共用的本地 control、PCM framing、背压及 terminal authority。

架构文档头部的 `last-calibrated` 指向 service-sdk 当前校准提交；跨仓提交在正文基线表中单独记录。
