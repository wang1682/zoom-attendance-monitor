# Identity Merge Rules

Zoom Monitor 身份归并规则。

## 自动归并（normalize_member_name）

由 `db.py` 中的 `normalize_member_name()` 自动处理。

**处理规则（按顺序）：**

1. 去掉 `(Host)` / `（Host）` 标记
2. 去掉身份关键词括号：`DC 值班号`、`duty`、`room`、`admin`、`host`
3. 去掉前后空格
4. 全部转小写
5. 全角半角统一：`～` → `~`
6. 连续空格压缩
7. 生成 `member_key`（去掉所有空格）

**覆盖的变体：**

- `Paisley~` / `Paisley～` / `Paisley ～` → `paisley`
- `Ceana` / `ceana (Host)` / `ceana ( DC 值班号 ) (Host)` → `ceana`
- `DC 值班号` / `DC 值班号 (Host)` → `dc值班号`
- `Oceanus` / `oceanus` → `oceanus`
- `cason` / `cason (Host)` → `cason`
- `Slanta` / `slanta` → `slanta`
- `Youngest` / `youngest` → `youngest`
- `unabell` / `unabell (Host)` → `unabell`
- `uanbell` / `uanbell (Host)` → `uanbell`

## 人工确认归并（Alias 表）

无法通过规则解决的、经业务确认同人，使用 Alias 表映射。

**定义位置：**

- `db.py` — `make_identity_key()` 内的 `ALIAS` 常量
- `admin_routes.py` — `_resolve_canonical()` 内的 `_ALIAS` 常量

**当前映射：**

| 源 member_key | 目标 member_key | 说明 |
|---|---|---|
| `antheafk` | `anthea` | `Anthea Fk` → `Anthea` |
| `harysonharyson` | `haryson` | `Haryson ( Hary Son )` → `Hary Son` |
| `crispin` | `crispini` | `crispin` → `Crispini` |
| `dcyoungest` | `youngest` | `DC youngest` → `Youngest` |
| `dcoceanus` | `oceanus` | `DCOceanus` → `oceanus` |

**新增 alias 流程：**

1. 在终端输出归并清单（raw_names → merged display_name）
2. 人工确认无误
3. 同时更新 `db.py` 的 `ALIAS` 和 `admin_routes.py` 的 `_ALIAS`

## 禁止归并

以下名称虽然相似，但经确认是不同人，**禁止合并**：

- `Noa` ≠ `Noal`
- `Maico` ≠ `Maicon`
- `Mikky` ≠ `Micky 爷`

## Email 聚合规则

- email 仅作为辅助身份字段
- 不单独作为强制合并依据
- 同 email 但不同 `member_key` 的条目走 fallback key，不强制合并
- 聚合算法见 `db.py` `make_identity_key()`

## 注意事项

1. `normalize_member_name` 和 `alias` 逻辑必须在以下两处**保持一致**：
   - `db.py` — 考勤矩阵 (`get_matrix`)
   - `admin_routes.py` — Official 页面汇总 (`_resolve_canonical`)
2. 新增 alias 前必须先输出归并清单给管理员确认
3. `participant_uuid` 不能用于跨会议身份识别（每次 meeting 会变）
4. `user_id` 覆盖率 100% 但受 Host 账户覆盖污染（多人共用同一个 Host 的 user_id），不能作为唯一标识
