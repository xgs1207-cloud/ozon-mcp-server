# 每晚弹性活动定时任务

每天 00:00 自动做三件事（四家店一起）：

1. **报弹性活动**：近 7 天上架的商品（`offer_id` 为 `xzj.M.D.n`，按北京时间日期算），
   如果在弹性活动候选池里、还没参加，就按**现价五折**报进去。
2. **清理其他活动**：扫描除弹性活动外的其他 Ozon 活动（比如 11.11 的超级/最大/Light Boosting），
   把 **Ozon 自动添加**的商品（`add_mode` 不是 `SELLER`）拉出活动；卖家自己加的商品不动。
3. **恢复价格**：被拉出的商品，价格恢复到它最近一笔 FBS 订单的成交价
   （订单 `financial_data` 的 `old_price`）；30 天内没有订单记录的跳过，不改价。

脚本：[`scripts/nightly_elastic.py`](../scripts/nightly_elastic.py)。
每次运行的日志写进 `logs/nightly-elastic/nightly-elastic-YYYY-MM-DD-HHMM.json`；
某次价格恢复失败的商品会记进 `logs/nightly-elastic/pending_price_restores.json`，
下次运行时自动重试。脚本只从 MCP 自己的加密店铺配置（`data/shops.json`）读密钥，
不碰任何明文密钥文件。

> 注意：恢复用的“进活动前成交价”实际是**最近一笔订单**的价格；如果商品最近的订单
> 本身就是在活动期间出的单，这个价就是活动期间的成交价。如要活动开始日之前的价格，
> 改脚本里 `last_order_price` 的取数窗口，或手动指定。

## 先跑一次试运行（必须做）

```bash
cd ozon-mcp-server
.venv/bin/python scripts/nightly_elastic.py --dry-run   # 只读，打印今晚会做什么
.venv/bin/python scripts/nightly_elastic.py --live      # 真正执行
```

`--dry-run` 不写任何东西：先看它列出的“会报几个、会拉出几个、会恢复几个价格”
跟你预期的是否一致，再决定要不要 `--live`。第一次 `--live` 跑完后，
用 `ozon_actions_products` 复查一遍活动参加列表，确认拉出的商品确实不在了。

## 用 Muse 设置定时

Muse 的定时任务由 Muse 自己创建和管理，操作就是在对话里下指令：

1. 把仓库克隆到放 MCP 的那台机器上（Muse 能访问到脚本的那台），按
   [USAGE.md](USAGE.md) 接好店铺并跑通一次 `--dry-run`。
2. 在 Muse 里新建一个对话，把下面“给 AI 的提示词”整段粘贴发给它。
3. Muse 建好定时任务后，让它把任务的下次运行时间、时区报给你核对
   （应为每天 00:00，北京时间 Asia/Shanghai）。
4. 第二天早上看它发来的汇报：每家店新报几个、拉出几个、价格恢复几个、有没有失败。
   失败的按汇报里的原因处理，不要让它自己反复重试写操作。

注意：Muse 的定时任务跑在它自己的环境里，脚本路径要写你这台机器上的**绝对路径**
（比如 `/home/hatch/workspace/custom-connectors/custom.ozon-mcp-server/scripts/nightly_elastic.py`），
不要写相对路径。

## 用 Workbuddy 设置定时

Workbuddy（腾讯的桌面 AI 助手）也有定时任务，叫“自动化定时任务”，两种建法：

1. **对话里建**：在 Workbuddy 对话框里直接说“每天 00:00 运行……”，把下面
   “给 AI 的提示词”整段发给它。
2. **手动建**：打开 **Scheduled tasks（定时任务）→ New（新建）**，填：
   - **Name（任务名）**：`每晚弹性活动报名与自动添加清理`
   - **Prompt（每轮执行内容）**：下面“给 AI 的提示词”整段
   - **Repeat, time and validity（重复、时间、有效期）**：每天（Daily），00:00

建好后先点 **Run** 手动跑一次，看结果对不对（就当是 `--live` 的试运行），
确认无误再让它每天自动跑；跑完的结果在运行历史里查，不对就点卡片上的开关暂停。

**关键限制（Workbuddy 官方说明）**：本地定时任务跑在你的电脑上，
电脑必须**开着、没休眠、Workbuddy 在运行**，到点才会执行；云端任务虽然不用开电脑，
但访问不到你电脑本地的文件（也就跑不了这个脚本）。所以这个任务请用**本地定时**，
并保证那台电脑每晚 00:00 是开着的。

> 依据：Workbuddy 自动化指南（Scheduled tasks → New：Name and prompt、
> Repeat, time and validity；先 Run 测试再保存）与产品说明
> （Scheduled automations 每日/每周/每月/每小时，带运行历史和测试运行；
> 本地任务依赖电脑开机运行）。

## 给 AI 的提示词（Muse / Workbuddy 通用，直接复制）

```text
请帮我设置一个每天 00:00（北京时间 Asia/Shanghai）运行的定时任务，任务内容是
维护我四家 Ozon 店铺的弹性活动。仓库在 <把这里换成你机器上仓库的绝对路径>，
脚本是 <仓库路径>/scripts/nightly_elastic.py，店铺密钥已经接进 MCP 的 data/shops.json，
不要让我再提供密钥，也不要把密钥写进任何文件或聊天记录。

每轮任务只做这一件事：用仓库虚拟环境的 Python 运行
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 <仓库路径>/.venv/bin/python <仓库路径>/scripts/nightly_elastic.py --live
脚本会：① 把近 7 天上架且在弹性活动候选池的商品按五折报进弹性活动；
② 把其他 Ozon 活动里 Ozon 自动添加的商品拉出活动（卖家自己加的不动）；
③ 被拉出的商品价格恢复到最近一笔订单的成交价。
运行前先看 docs/NIGHTLY-TASK.md 和 docs/USAGE.md 了解规则。

跑完后用中文表格向我汇报：每家店新报几个、拉出几个、价格恢复几个，
失败的写明原因。如果脚本报错导致某家店整段失败，不要自己重试写操作，
先汇报失败情况等我决定。如果什么都没发生，只发一行“今晚无变化”。
```

把 `<仓库路径>` 换成实际路径（比如 `/home/hatch/workspace/custom-connectors/custom.ozon-mcp-server`）
再发。第一次建任务前，先手动跑一遍 `--dry-run` 确认脚本列出的操作符合预期。

## 常见问题

- **今晚一个都没报**：正常。近 7 天没有新上架、或新上的都已经在活动里了，脚本就什么都不报。
- **拉出了商品但价格没恢复**：先看日志里的 `errors`，常见原因是 30 天内没有该商品的
  FBS 订单（拿不到成交价），或改价接口报错；失败的会进 pending 文件，下次自动重试。
- **改价报错 `currency_changed`**：脚本已处理（改价带 `currency_code`）；如果还出现，
  检查店铺币种是不是 CNY 以外的币种。
- **某家店整段失败**：先看日志，确认是网络/密钥问题还是脚本问题；不要反复 `--live`，
  写操作失败重跑可能重复执行。
- **想停掉**：在 Muse 里删除/暂停该定时任务，或在 Workbuddy 里关掉任务卡片的开关；
  脚本本身不常驻，没有后台进程。
