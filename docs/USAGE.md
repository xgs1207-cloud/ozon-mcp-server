# Ozon MCP 使用指南

> 本仓库是 [DeviceIngineering/ozon-mcp-server](https://github.com/DeviceIngineering/ozon-mcp-server)
> v2.5.2 的 fork，改动见 [CHANGELOG.md](../CHANGELOG.md)：主要是 **Actions（促销）接口已更新到
> Ozon 统一限价版**（读走 `/v2/actions/*`，写走 `/v1/actions/products/update`），
> 另加一个每晚自动跑的弹性活动脚本（见 [NIGHTLY-TASK.md](NIGHTLY-TASK.md)）。
> 上游原版安装与客户端配置见 [README.zh.md](../README.zh.md)，下面只写**这个 fork 的用法和坑**。

## 快速开始

```bash
git clone https://github.com/xgs1207-cloud/ozon-mcp-server.git
cd ozon-mcp-server
python3 -m venv .venv && .venv/bin/pip install -e .
```

启动服务（SSE + 网页管理界面）：

```bash
./ozon-mcp-run.sh        # 监听 :8000，DATA_DIR 默认 ./data
```

打开 http://localhost:8000/shops 接入店铺，每家店填：

| 字段 | 从哪拿 |
|---|---|
| Seller API `Client-Id` + `Api-Key` | seller.ozon.ru → 设置 → API 密钥 → Seller API |
| Performance（广告）API `client_id` + `client_secret` | 同一页 → Performance API 标签，`client_id` 形如 `xxx@advertising.performance.ozon.ru` |

密钥经 Fernet 加密存进 `data/shops.json`，**不要把 `data/` 提交到 git**（已在 `.gitignore`）。
保存后点每家店的 **Test**：Seller 和 Performance 都显示 OK 才算接通。
`ozon_list_shops` 能列出所有已接店铺；其余所有工具调用都要带 `shop_id`。

## 工具总览（156 个）

| 大类 | 能干什么 | 常用工具 |
|---|---|---|
| 促销活动 | 查活动/候选/已参加商品；报名、改价、退出；自建卖家促销 | `ozon_actions_list`、`ozon_actions_candidates`、`ozon_actions_products`、`ozon_actions_activate`、`ozon_actions_deactivate` |
| 价格 | 改价、查价和价格指数、最低价保护 | `ozon_set_prices`、`ozon_get_prices`、`ozon_min_price_timer_status`、`ozon_min_price_timer_renew` |
| 价格策略 | 按竞品自动调价 | `ozon_pricing_strategy_list/create/update`、`ozon_pricing_competitors` |
| 商品 | 列表/详情/属性/库存/证书；批量导入、改图、改库存、上下架 | `ozon_product_list`、`ozon_product_info`、`ozon_product_import`、`ozon_product_update_images`、`ozon_product_update_stocks` |
| 订单 | FBS/FBO 查单、发货、打标签、交接单 | `ozon_orders_fbs`、`ozon_order_fbs_ship`、`ozon_order_fbs_label`、`ozon_orders_fbo` |
| 退货/取消 | FBO+FBS 统一退货列表、rFBS 审批、取消申请 | `ozon_returns_fbo`、`ozon_returns_fbs`、`ozon_returns_fbs_approve/reject`、`ozon_cancellation_list` |
| 客服 | 评价回复、买家问题、买家聊天 | `ozon_reviews`、`ozon_review_reply`、`ozon_questions`、`ozon_chat_list/history/send` |
| 广告 | CPC/CPO 系列管理、出价、预算、统计 | `ozon_ad_campaigns`、`ozon_ad_statistics`、`ozon_ad_campaign_bids`、`ozon_search_promo_products`、`ozon_search_promo_enable/disable` |
| 财务 | 交易明细、月结、余额、现金流 | `ozon_finance_transactions`、`ozon_finance_totals`、`ozon_finance_balance`、`ozon_finance_realization` |
| 分析 | SKU 销量、库存分析、搜索词与搜索位置 | `ozon_analytics`、`ozon_analytics_stocks`、`ozon_product_queries`、`ozon_search_queries_top` |
| 其他 | FBO 供货单、仓库、报表、类目树、证书、诊断 | `ozon_supply_orders`、`ozon_report_*`、`ozon_category_tree`、`ozon_diagnostics` |

## 常用操作示例

**查某家店弹性活动的候选商品（还没参加、Ozon 打算拉进去的）：**

```
ozon_actions_candidates(shop_id="snapcross", action_id=1977747, limit=100)
```

返回每件商品的现价 `price`、最高限价 `max_action_price`、币种（CNY 店返回 CNY），
以及 `last_id`（翻页用）。**先用这个接口拿限价，再报名**——报名价高于限价会被
Ozon 理解成“退出活动”，而不是报名。

**报名（弹性活动，五折为例）：**

```
ozon_actions_activate(shop_id="snapcross", action_id=1977747,
  products=[{"product_id": 6981138391, "action_price": 77.5}])
```

返回 `active_product_ids` 即成功；`rejected` 里有原因。本 fork 的报名实现会先按限价
校验：价格 ≤ 0、币种未知、价格高于限价的商品在发送前就被拒绝，不会误发成退出。

**退出活动：**

```
ozon_actions_deactivate(shop_id="snapcross", action_id=1977747, product_ids=[6981138391])
```

弹性/最大 Boosting 和库存折扣没有直接的退出接口，退出是把活动价设成高于限价
（限价向下取整 +1）；促销码类活动走 `/v2/actions/products/deactivate`。
本 fork 的退出实现自动处理这两种情况。**注意：退出后商品回到卡片原价**；
如果另一场活动改过卡片原价本身，退出后不会自动恢复，需要按订单记录手动改价
（`ozon_orders_fbs` 的 `financial_data` 里有每单的 `price` / `old_price`，
即当时的成交价；`ozon_set_prices` 改价，见下）。

**改价：**

```
ozon_set_prices(shop_id="snapcross",
  prices=[{"offer_id": "xzj.11.14.1", "price": "50", "currency_code": "CNY"}])
```

**`currency_code` 必填**：CNY 店不传它会报 `currency_changed`（Ozon 默认按 RUB 理解），
`updated: false`，价格根本没改。改完要核对返回的 `updated` 字段。

**查过去的成交价（忘了原价时）：**

```
ozon_orders_fbs(shop_id="snapcross", since="2026-11-01T00:00:00Z", to="2026-11-14T23:59:59Z", limit=50)
```

每单的 `financial_data.products[]` 有 `price`（当时售价）、`old_price`（折前价）、
`customer_price`（买家实付，卢布）、`total_discount_percent` 和 `actions`
（当时生效的活动名）。按 SKU 筛出进活动之前的订单，`old_price` 就是进活动前的成交价。

**查财务（最近 30 天）：**

```
ozon_finance_totals(shop_id="snapcross", date_from="2026-10-15", date_to="2026-11-14")
ozon_finance_balance(shop_id="snapcross")
```

前者按服务拆费用（跨境物流、CPO/CPC、收单、订阅等），后者给期初/期末余额、
本期到账和提现。注意“净到账”是 Ozon 侧扣完佣金物流广告后的数，还没扣进货成本，
不等于利润。

## 必须知道的坑

1. **列表返回会被截断**：MCP 对每个工具的返回做了 60,000 字符上限，超长列表会被静默截掉
   一部分（曾出现 672 个商品只返回 479 个）。“列表里没找到”不等于“不存在”。
   大列表用 `limit` + `last_id` 分页拉，或直接调 Seller API 核对。
2. **弹性活动的候选记录里没有 `offer_id`/`sku`**：`actions_candidates` 返回的记录只有
   `id`（product_id）、价格和限价，没有 offer_id。想按 offer_id（比如 `xzj.11.16.x`）
   筛候选商品，必须先拿 `product_info_list` 把 product_id 换成 offer_id
   再匹配——直接在候选记录上匹配 offer_id 永远匹配不到（2026-11-17 实测：
   每晚脚本因此连续两晚报了 0 个，漏掉了 66 个商品，修好后一次补报成功）。
2. **Actions 接口已换版**：读用 `/v2/actions/candidates`、`/v2/actions/products`，
   写用 `/v1/actions/products/update`；旧的 `/v1/actions/products/activate`、
   `/v1/actions/products/deactivate` 自 2026-10-19 起被 Ozon 封了（400）。
   本 fork 已改好，不要再按旧文档调。
3. **报名价高于限价 = 退出**，不是报名。报名之前先查 `max_action_price`。
4. **退出活动的返回不可全信**：从一个活动退出，有时会连带把商品从另一个活动也退出，
   导致返回里没列出它；退出后务必重新拉一次参加列表确认。
5. **Performance 401 `invalid_client` 基本都是 client_id 和 secret 不是同一对**：
   换一对重新生成的、或核对后台显示的 client_id 再试；同一 secret 配错 client_id 也会 401。
6. **价格相关**：`ozon_set_prices` 必须带 `currency_code`；最低价保护
   （`min_price` + 30 天计时器）能防止活动把价格打到成本以下，计时器到期要续。
7. **网络**：直连 github.com:22 / :443 可能被出口代理重置，SSH 走
   `ssh.github.com:443` + HTTP 代理；同时 `NO_PROXY` 里不能有裸 `::1`，
   否则 Python httpx 报 `InvalidURL`——启动脚本已处理（`NO_PROXY=localhost,127.0.0.1`）。
8. **一个商品同一时间只能参加一个 Ozon 折扣活动**：进了新活动，Ozon 会自动把它从旧活动
   里移出；Ozon 也会自动往活动里加商品（`add_mode` 不是 `SELLER` 的就是自动加的）。
   每晚自动处理见 [NIGHTLY-TASK.md](NIGHTLY-TASK.md)。
