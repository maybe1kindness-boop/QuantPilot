# NOTICE — 项目来源与致谢

QuantPilot 是一个独立的个人量化交易系统项目，在以下 MIT 协议开源项目的基础上二次开发而来：

## 上游项目

| 项目 | 地址 | 许可证 | 贡献 |
|------|------|--------|------|
| KHunter | https://github.com/ling-0729/KHunter | MIT | 直接的上游基础：akshare 数据获取框架、部分选股策略、技术指标计算与 Web 框架 |
| a-share-quant-selector | https://github.com/Dzy-HW-XD/a-share-quant-selector | MIT | KHunter 的上游项目，基础架构原作者 |

## 本项目的扩展

在上述基础架构之上，QuantPilot 新增/重写了以下主要部分：

- 选股策略库扩展与形态匹配（`strategy/`）
- 回测引擎与回测研究框架（`trading/backtest_engine.py`、`backtest_2y/`）
- 自选股管理与策略跟进（`trading/khunter_*`、`paper_trading/watchlist.py`）
- Web 工作台与数据管理链路（`web_server.py`、`web/`、`utils/`）

## 声明

- 感谢上游项目作者 **ling-0729** 与 **Dzy-HW-XD** 的开源贡献
- 依据 MIT 许可证条款，本项目分发时保留上游版权与许可声明；上游项目的完整许可证文本可见其仓库
- QuantPilot 为独立项目，与上述上游项目及其作者无隶属关系
- 本项目仅供学习研究，全部交易为模拟交易，不构成投资建议
