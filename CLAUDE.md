# 项目约定

## 数据仓库命名规范

> 新建的表和字段一律按本规范。既有的 `src_opinion_*` 表不动（历史表，改名会丢数据）。

### 命名风格

- 全小写 + 下划线：`snake_case`。
- **表名**：`分层_业务域_实体名_粒度`，例：`dwd.dwd_ord_ota_booking_di`（OTA 预订订单明细天表）。
- **字段名**：`实体_属性`，例：`flight_no`、`hotel_id`、`checkin_date`。
- 代码里引用表**必须带库名**：`select * from dwd.dwd_ord_ota_booking_di`。
- 表 DDL **字段必须有注释，表也要有注释**。
- 分区字段默认用时间字段，统一 `travel_date`，格式 `YYYYMMDD`。

### 命名原则

1. 清晰明了：业务、非业务的人看名字也能明白是什么，不只给技术看。
2. 类型 / 状态类字段，注释里**必须写明每个取值的含义**，如 `0-成功 1-失败`。
3. **不许用拼音**，必须是英文单词或缩写（规范里列出的关键字除外）。
4. 代码中表必须「库 + 表」组合引用。
5. 分区字段默认时间字段，统一 `travel_date`（`YYYYMMDD`）。

### 主题域与表前缀

| 主题域 | 说明 | 前缀 |
| :--- | :--- | :--- |
| product | 产品（机票、酒店、度假） | `prd_` |
| order | 订单、预订 | `ord_` |
| customer | 用户、旅客 | `cus_` |
| supplier | 供应商（酒店、航司、地接） | `sup_` |
| inventory | 库存（房量、机位） | `inv_` |
| finance | 财务、结算 | `fin_` |
| operation | 运营（航班、酒店房态） | `opr_` |
| traffic | 流量、渠道 | `trf_` |
| marketing | 营销、活动 | `mkt_` |

### 数据分层前缀

| 层级 | 含义 | 前缀 |
| :--- | :--- | :--- |
| ODS | 原始数据层 | `ods_` |
| DWD | 明细层（清洗、规范化） | `dwd_` |
| DWS | 汇总层（主题宽表） | `dws_` |
| ADS | 应用层（报表、指标） | `ads_` |
| DIM | 维度表 | `dim_` |
| SRC | 业务源层 | `src_` |

### 时间粒度与表后缀

| 时间粒度 | 增量后缀 | 全量后缀 | 说明 |
| :--- | :--- | :--- | :--- |
| 小时 | `_hrs` | `_hrs_f` | 小时级增量 / 小时级全量快照 |
| 天 | `_di` | `_df` | 天级增量 (daily increment) / 天级全量 (daily full) |
| 周 | `_wk` | `_wk_f` | 周级增量 / 周级全量快照 |
| 月 | `_mo` | `_mo_f` | 月级增量 / 月级全量快照 |
| 季度 | `_q` | `_q_f` | 季度级增量 / 季度级全量快照 |
| 年 | `_yr` | `_yr_f` | 年级增量 / 年级全量快照 |
| 无周期（全量） | — | `_f` | 非分区全量表 |
| 拉链表 | — | `_zip` | 记录历史变更的拉链表 |

### 字段命名关键字

| 关键字 | 含义 | 实例 |
| :--- | :--- | :--- |
| `_code` | 代码（机场三字码、酒店代码） | `user_code` |
| `_no` | 编号（订单号、票号） | `invoice_no` |
| `_name` | 名称 | `prd_name` |
| `_type` | 类型 | `invoice_type` |
| `_status` | 状态 | `invoice_status` |
| `_date` | 日期（无时间） | `invoice_date` |
| `_time` | 时间（含时分秒 / 毫秒） | `invoice_time` |
| `_amt` | 金额 | `invoice_amt` |
| `_qty` | 数量 | `ticket_qty` |
| `_rate` | 比例、费率、占比 | `adult_rate` |

### 通用业务术语

| 术语 | 说明 |
| :--- | :--- |
| `ota` | OTA（携程、飞猪） |
| `gds` | 分销（Amadeus、Sabre） |
| `pnr` | 预定 |
| `inventory` | 库存（房量、机位、车位等） |
| `supplier` (`sup`) | 供应商 |
| `product` (`prd`) | 产品（机票、酒店、套餐等） |
| `combo` | 组合产品（机+酒、跟团游） |
| `booking` | 预订 |
| `_order_` (`ord`) | 订单 |
| `traveler` | 旅客 / 游客 |
| `ticket` | 票 |
| `price` | 价 |
| `type` | 类型 |
| `leg` | 航程 / 里程 |
| `level` | 等级 |
| `tour` | 旅游团产品 |
| `attraction` | 景点 |
| `net` | 结算类价格 |
| `up` | 增加 |
| `down` | 减少 |
| `cancel` | 取消 |
| `settlement` | 结算 |
| `invoice` | 发票类 |
| `category` | 分类 |
