# QuantPilot — A 股自定义选股与回测工作台

QuantPilot 是一套面向个人投资者的 A 股**自定义选股策略 + 回测验证**系统：在网页上写自己的选股策略、保存管理、立即执行筛选，并直接对策略做历史回测；同时内置 16 种选股策略开箱可用。全部数据来自公开行情接口，不接入任何交易通道。

## 🧩 自定义选股策略（核心）

- **网页代码编辑器写策略**：只需定义一个 `match_stock(df, code, name)` 函数，对全市场股票的 K 线 DataFrame 返回是否命中（可附命中理由）
- **策略保存与管理**：自定义策略本地持久化，支持列表、保存、删除，跨会话可用
- **立即执行筛选**：保存的策略一键对全市场约 5200 只股票执行，按需自动下载历史 K 线
- **自然语言规则模式**：支持用规则描述选股条件，内置强度评分（涨停距离/连板结构/上影线/换手分项合成）
- 示例：

```python
def match_stock(df, code, name):
    # df: 该股日K线(日期/开高低收/量), 倒序排列
    close = df['close']
    if len(close) < 60:
        return False
    ma20 = close.rolling(20).mean()
    return bool(close.iloc[0] > ma20.iloc[0] > ma20.iloc[5])
```

## 🧪 回测验证

- **保存的策略直接作为回测源**：对自定义策略一键回测，回测配置（策略/区间/择时/参数）增删改查、单次与批量运行、进度跟踪、净值曲线与交易明细
- **引擎级回测**：`trading/backtest_engine.py` 按策略参数在历史数据上复现信号与买卖
- **回测研究框架**：`backtest_2y/` 下 30+ 个独立研究脚本（多年回放、策略组合、参数扫描、Excel 报告）

## 📦 内置 16 种选股策略（开箱可用）

金三角、仙人指路、W 底、阻力位突破、趋势共振反转、启明星、金叉不绿、多金叉共振、趋势加速拐点、强势洗盘弱转强、2560 战法、趋势起点、多方炮、涨停回马枪、底部趋势拐点、涨停横盘——可在选股执行页勾选启用、组合运行，并附形态库 DTW 历史形态匹配（`pattern_library` / `pattern_matcher`），每个策略的说明见 `strategy/strategy_specifications.md`。

## ⭐ 自选股跟踪

- 自选股清单管理（增删改、本地持久化），选股结果一键加入自选
- 自选股策略跟进：对关注列表按策略持续评分跟踪（狩猎场 / 跟踪页）
- 历史选股记录可回看

## 🌐 Web 工作台

Flask + SocketIO 实时界面（默认端口 5001），16 个功能页：数据初始化 / 数据更新 / 选股执行 / 选股历史 / 结果分析 / 股票排行 / 排行跟踪 / 回测配置 / 回测历史 / 策略管理 / 自选跟踪等。

## 📊 数据层

- 多数据源自动降级：TickFlow → 腾讯财经（日线）、东财 → AKShare → 腾讯（实时行情），Tushare 可选
- 日线增量更新，全市场约 5200 只股票，SQLite 本地存储，建表脚本随仓库提供
- 网页端一键初始化：实时进度条、支持暂停/继续

## 目录结构

```
├── main.py                  # 入口: init初始化 / update更新 / run选股 / web服务 / schedule定时
├── web_server.py            # Web 工作台 (Flask, :5001)
├── strategy/                # 选股策略库(20+种)
├── trading/                 # 回测引擎/评分/自选跟踪
├── paper_trading/           # 自选股持久化(watchlist)
├── web/                     # 前端模板与静态资源
├── utils/                   # 数据抓取与工具库
├── config/                  # 配置(含 config.yaml.template)
├── backtest_2y/             # 回测研究脚本
├── runtime/data/*.sql       # 数据库建表脚本
└── tests/                   # 测试
```

## 快速开始

### 方式一：双击启动（推荐）

| 平台 | 启动文件 |
|------|---------|
| Windows | 双击 `start_web.bat` |
| macOS | 双击 `启动Web面板.command` |

- 首次双击会自动创建虚拟环境并安装依赖，之后启动很快
- 只需已安装 Python 3.10+（Windows 安装时勾选 *Add to PATH*）
- 建议通过 `git clone` 获取项目；若下载 ZIP，macOS 双击 `.command` 可能提示"未知开发者"，右键 → 打开 即可

### 方式二：命令行

```bash
# 1. 安装依赖 (Python 3.10+)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. 创建本地配置(不填钉钉 webhook 也能正常使用)
cp config/config.yaml.template config.yaml

# 3. 启动 Web 工作台
python3 web_server.py        # 浏览器打开 http://127.0.0.1:5001

# 或使用命令行流程
python3 main.py init         # 首次: 下载全市场历史数据(约1-3小时)
python3 main.py update       # 此后每日增量更新
python3 main.py run          # 执行选股
```

### 首次使用须知（重要）

1. **首次启动后请先初始化数据**：打开 Web 工作台 → 数据管理 → 点击「数据初始化」，自动下载全市场约 5200 只股票的基础信息与历史 K 线（视网络约 1~3 小时，有进度条，支持暂停/继续）
2. 初始化完成前，选股与回测因缺少历史数据，结果为空属正常现象
3. 之后每次使用会自动增量更新当日数据
4. 命令行 `python main.py update` 在空数据库上是空操作——**数据初始化必须先行**
5. 可选：在 `config/tushare_config.json` 中写入 `{"token": "你的token"}` 可启用 Tushare 数据源（免费注册），未配置时自动使用腾讯财经等免费源

## ⚠️ 免责声明

本项目仅供量化学习与研究使用，不构成任何投资建议；股市有风险，据此操作产生的任何损失作者不承担责任。

## 💬 交流与反馈

- 个人 QQ：**1140779706**（欢迎交流使用问题与量化思路，请注明"QuantPilot"）
- 想定制更多功能（新增策略、个性化改造、部署支持等），请联系博主，支持有偿定制开发

## 📄 许可与致谢

本项目基于 MIT 协议开源（见 [LICENSE](LICENSE)）。

QuantPilot 在以下开源项目的基础上二次开发而来，特此致谢（详见 [NOTICE](NOTICE.md)）：

- [ling-0729/KHunter](https://github.com/ling-0729/KHunter)（MIT）
- [Dzy-HW-XD/a-share-quant-selector](https://github.com/Dzy-HW-XD/a-share-quant-selector)（MIT）
