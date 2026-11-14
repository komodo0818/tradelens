"""TradeLens — 零基础模拟炒股训练舱

目标用户：完全没有炒股经验的学生。
用 10 万虚拟资金练习买卖，看懂持仓、盈亏和自己的交易习惯，不花一分真钱。
"""

import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    import akshare as ak
except Exception:          # 没装或环境异常时，页面依然能打开
    ak = None

# ---------------------------------------------------------------------------
# 基础配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = os.path.join(BASE_DIR, "trades.csv")
PRICES_FILE = os.path.join(BASE_DIR, "prices.json")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
PRICE_CACHE_DIR = os.path.join(BASE_DIR, "price_cache")
FAIL_BACKOFF = timedelta(minutes=10)   # 请求失败后的冷却期，避免越重试越被限流

INITIAL_CAPITAL = 100_000.00   # 初始虚拟资金
LOT_SIZE = 100                 # A股一手 = 100 股
MILK_TEA_PRICE = 15.0          # 一杯奶茶按 15 元估算，用来把盈亏讲成人话

COLUMNS = ["股票代码", "买卖方向", "交易日期", "价格", "数量", "手续费", "成交金额"]
DIRECTIONS = ["买入", "卖出"]


# ---------------------------------------------------------------------------
# 数据读写
# ---------------------------------------------------------------------------
def load_trades() -> pd.DataFrame:
    """读取 trades.csv；文件不存在或为空时返回空表。"""
    if not os.path.exists(TRADES_FILE):
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(TRADES_FILE, dtype={"股票代码": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=COLUMNS)

    for col in COLUMNS:            # 兼容旧文件缺列的情况
        if col not in df.columns:
            df[col] = None
    return df[COLUMNS]


def append_trade(record: dict) -> None:
    """追加一条交易到 trades.csv，文件不存在则自动创建（含表头）。"""
    file_exists = os.path.exists(TRADES_FILE) and os.path.getsize(TRADES_FILE) > 0
    df = pd.DataFrame([record], columns=COLUMNS)
    df.to_csv(
        TRADES_FILE,
        mode="a",
        header=not file_exists,
        index=False,
        encoding="utf-8-sig",      # 带 BOM，Excel 打开中文不乱码
    )


def load_prices() -> dict:
    """读取手动维护的现价表，返回 {股票代码: 现价}。"""
    if not os.path.exists(PRICES_FILE):
        return {}
    try:
        with open(PRICES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): float(v) for k, v in data.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_prices(prices: dict) -> None:
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)


def reset_account() -> list:
    """把现有记录归档到 backups/ 后清空，返回被归档的文件名。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    # 微秒级时间戳：同一秒内连续载入案例/重置时，备份不会互相覆盖
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    moved = []
    for path in (TRADES_FILE, PRICES_FILE):
        if not os.path.exists(path):
            continue
        name, ext = os.path.splitext(os.path.basename(path))
        target = os.path.join(BACKUP_DIR, f"{name}_{stamp}{ext}")
        os.replace(path, target)
        moved.append(os.path.basename(target))
    return moved


# ---------------------------------------------------------------------------
# 新手体验区：经典教学案例
# ---------------------------------------------------------------------------
def _demo_row(symbol, direction, day, price, qty, fee) -> dict:
    return {
        "股票代码": symbol,
        "买卖方向": direction,
        "交易日期": day,
        "价格": price,
        "数量": qty,
        "手续费": fee,
        "成交金额": round(price * qty, 2),
    }


def build_demo_trades(case: str) -> tuple:
    """生成教学案例的交易记录和配套现价表。返回 (DataFrame, prices_dict)。"""
    today = date.today()

    def d(days_ago: int) -> str:
        return (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    if case == "hyper":
        # 案例一：多动症短线客 —— 平安银行 10 笔频繁进出，
        # 每次赚小钱亏小钱，10 次手续费 50 元把利润全吃掉
        rows = [
            _demo_row("000001", "买入", d(10), 12.00, 1000, 5.0),
            _demo_row("000001", "卖出", d(9), 12.06, 1000, 5.0),   # +55
            _demo_row("000001", "买入", d(8), 12.02, 1000, 5.0),
            _demo_row("000001", "卖出", d(7), 11.94, 1000, 5.0),   # -85
            _demo_row("000001", "买入", d(6), 11.90, 1000, 5.0),
            _demo_row("000001", "卖出", d(5), 11.96, 1000, 5.0),   # +55
            _demo_row("000001", "买入", d(4), 12.00, 1000, 5.0),
            _demo_row("000001", "卖出", d(3), 12.10, 1000, 5.0),   # +95
            _demo_row("000001", "买入", d(2), 12.08, 1000, 5.0),
            _demo_row("000001", "卖出", d(1), 12.00, 1000, 5.0),   # -85
        ]
        return pd.DataFrame(rows), {"000001": 12.00}

    if case == "yolo":
        # 案例二：全仓梭哈勇士 —— 茅台一手 13 万+，10 万本金根本不够，
        # 硬买把现金干成负数，跌了不卖还补仓，越补越深
        rows = [
            _demo_row("600519", "买入", d(9), 1400.00, 100, 42.0),
            _demo_row("600519", "买入", d(1), 1380.00, 100, 41.4),
        ]
        return pd.DataFrame(rows), {"600519": 1380.00}

    if case == "panic":
        # 案例三：追涨杀跌小白 —— 大涨追高买，跌一点就割肉，回本了又忍不住再追
        rows = [
            _demo_row("000001", "买入", d(20), 12.00, 1000, 5.0),
            _demo_row("000001", "买入", d(15), 13.20, 1000, 5.0),  # 大涨后追高
            _demo_row("000001", "卖出", d(10), 12.30, 1000, 5.0),  # 恐慌割肉
            _demo_row("000001", "买入", d(5), 13.00, 1000, 5.0),   # 又忍不住追
        ]
        return pd.DataFrame(rows), {"000001": 13.00}

    raise ValueError(f"未知案例: {case}")


def apply_demo_case(case: str) -> tuple:
    """载入教学案例：先备份现有数据，再用 to_csv 覆盖写入。返回 (备份文件名, 摘要)。"""
    archived = reset_account()
    df, demo_prices = build_demo_trades(case)
    df.to_csv(TRADES_FILE, index=False, encoding="utf-8-sig")   # 覆盖写入（含表头）
    save_prices(demo_prices)
    return archived, f"共 {len(df)} 笔交易"


# ---------------------------------------------------------------------------
# 账户计算
# ---------------------------------------------------------------------------
def build_account(trades: pd.DataFrame, prices: dict) -> dict:
    """按时间顺序回放所有交易，算出账户状态。

    成本价采用「移动加权平均」，手续费计入买入成本 —— 对新手来说最好理解：
    买一只股票总共花了多少钱，除以股数，就是你的成本价。
    """
    cash = INITIAL_CAPITAL
    realized = 0.0                 # 已实现盈亏（卖出时兑现的部分）
    positions = {}                 # 代码 -> [股数, 成本价]
    last_price = {}                # 代码 -> 最近一次成交价

    for _, row in trades.iterrows():
        try:
            symbol = str(row["股票代码"]).strip().upper()
            qty = int(row["数量"])
            price = float(row["价格"])
            fee = float(row["手续费"] or 0)
        except (TypeError, ValueError):
            continue                   # 跳过脏数据，不让整个页面崩掉
        if not symbol or qty <= 0 or price <= 0:
            continue

        amount = price * qty
        last_price[symbol] = price
        pos = positions.setdefault(symbol, [0, 0.0])

        if row["买卖方向"] == "买入":
            cash -= amount + fee
            new_qty = pos[0] + qty
            # 成本价 = 累计买入总成本 / 总股数（含手续费）
            pos[1] = (pos[1] * pos[0] + amount + fee) / new_qty
            pos[0] = new_qty
        else:
            cash += amount - fee
            sold = min(qty, pos[0])
            realized += (price - pos[1]) * sold - fee
            pos[0] -= qty
            if pos[0] <= 0:            # 清仓后成本归零，避免残留
                pos[0] = 0
                pos[1] = 0.0

    # 组装持仓明细
    holdings = []
    market_value = 0.0
    unrealized = 0.0
    for symbol, (qty, cost) in positions.items():
        if qty <= 0:
            continue
        current = prices.get(symbol) or last_price.get(symbol) or cost
        mv = current * qty
        pnl = (current - cost) * qty
        pct = pnl / (cost * qty) if cost > 0 else 0.0

        market_value += mv
        unrealized += pnl
        holdings.append({
            "symbol": symbol,
            "qty": qty,
            "cost": cost,
            "price": current,
            "mv": mv,
            "pnl": pnl,
            "pct": pct,
            "plain": pnl_plain(pnl, pct),
        })

    total = cash + market_value
    total_pnl = total - INITIAL_CAPITAL
    return {
        "cash": cash,
        "market_value": market_value,
        "total": total,
        "unrealized": unrealized,
        "realized": realized,
        "total_pnl": total_pnl,
        "total_pct": total_pnl / INITIAL_CAPITAL if INITIAL_CAPITAL else 0.0,
        "holdings": holdings,
        "last_price": last_price,
    }


# ---------------------------------------------------------------------------
# 把盈亏翻译成大白话
# ---------------------------------------------------------------------------
def pnl_plain(pnl: float, pct: float) -> str:
    """把冷冰冰的数字，讲成学生一听就懂的话。"""
    if pnl > 0:
        cups = pnl / MILK_TEA_PRICE
        if cups < 1:
            return f"赚了 {pnl:,.0f} 元，还不到一杯奶茶 ☕"
        if cups < 5:
            return f"赚了约 {cups:.1f} 杯奶茶的钱 🧋"
        if cups < 30:
            return f"赚了约 {cups:.0f} 杯奶茶 🧋"
        if cups < 100:
            return f"赚了 {cups:.0f} 杯奶茶，够请全班喝一轮 🎉"
        return f"赚了 {cups:.0f} 杯奶茶，可以换台新手机了 📱"

    if pnl < 0:
        cups = abs(pnl) / MILK_TEA_PRICE
        if pct <= -0.10:
            return f"亏了约 {cups:.0f} 杯奶茶，已亏损超 10%，该考虑止损了 ⚠️"
        if pct <= -0.05:
            return f"亏了约 {cups:.0f} 杯奶茶，想想当初买入的理由还成立吗 🤔"
        if cups < 1:
            return "小亏一点点，问题不大，先观察 👀"
        return f"小亏约 {cups:.0f} 杯奶茶，别慌，继续观察 👀"

    return "不赚不亏，先观望"


# ---------------------------------------------------------------------------
# 交易校验
# ---------------------------------------------------------------------------
def validate_trade(symbol, direction, price, quantity, fee, cash, held_qty) -> list:
    """返回错误信息列表，空列表表示通过。"""
    errors = []
    if not symbol.strip():
        errors.append("股票代码不能为空，例如 600519（贵州茅台）、000001（平安银行）。")
    if price <= 0:
        errors.append("价格必须大于 0。")
    if quantity <= 0:
        errors.append("数量必须大于 0。")
    elif quantity % LOT_SIZE != 0:
        errors.append(
            f"A股数量必须是 {LOT_SIZE} 的整数倍（1 手 = 100 股），你填的是 {quantity} 股。"
        )

    if errors:
        return errors

    amount = price * quantity
    if direction == "买入":
        if amount + fee > cash + 1e-6:
            errors.append(
                f"现金不够：这笔需要 {amount + fee:,.2f} 元，你只有 {cash:,.2f} 元。"
            )
    else:
        if quantity > held_qty:
            errors.append(
                f"持仓不够：你只持有 {held_qty} 股，不能卖出 {quantity} 股。"
            )
    return errors


# ---------------------------------------------------------------------------
# 新手快捷下单
# ---------------------------------------------------------------------------
QUICK_STOCKS = {
    "贵州茅台 (600519)": "600519",
    "平安银行 (000001)": "000001",
    "宁德时代 (300750)": "300750",
}
QUICK_FEE_RATE = 0.0003      # 万分之三
QUICK_FEE_MIN = 5.0          # 不足 5 元按 5 元收


def compute_quick_fee(amount: float) -> float:
    """快捷下单手续费：成交金额 × 万分之三，不足 5 元按 5 元收。"""
    return round(max(QUICK_FEE_MIN, amount * QUICK_FEE_RATE), 2)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_live_price(symbol: str):
    """尝试获取实时价格，返回 (价格, 来源) 或 (None, 失败原因)。全程 try...except。

    第一优先：东方财富五档实时接口（stock_bid_ask_em，取「最新」价）；
    第二优先：当日日 K 的收盘价（stock_zh_a_hist）。
    两个接口都是东财的，限流时一起失败——上层会走本地缓存兜底。
    结果缓存 5 分钟，避免连续点按钮把接口打限流。
    """
    if ak is None:
        return None, "未安装 akshare"

    # —— 第一次尝试：五档实时报价 ——
    try:
        df = ak.stock_bid_ask_em(symbol=symbol)
        if df is not None and not df.empty and df.shape[1] >= 2:
            name_col, value_col = df.columns[0], df.columns[1]
            for _, row in df.iterrows():
                if str(row[name_col]) in ("最新", "最新价", "现价"):
                    value = float(row[value_col])
                    if value > 0:
                        return value, "实时行情"
    except Exception:
        pass                        # 限流、网络错误、代码不存在——不崩溃，继续往下试

    # —— 第二次尝试：当日日 K 收盘价 ——
    try:
        raw = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=date.today().strftime("%Y%m%d"),
            end_date=date.today().strftime("%Y%m%d"),
            adjust="",
        )
        if raw is not None and not raw.empty and "收盘" in raw.columns:
            value = float(raw["收盘"].iloc[-1])
            if value > 0:
                return value, "今日收盘价"
    except Exception:
        pass

    return None, "实时行情不可用（接口限流或网络错误）"


def cached_history_price(symbol: str):
    """从本地行情缓存（price_cache/{代码}.csv）取最近一个收盘价，不联网。"""
    path = os.path.join(PRICE_CACHE_DIR, f"{symbol}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        if df.empty or "close" not in df.columns:
            return None
        value = float(df["close"].iloc[-1])
        return value if value > 0 else None
    except (ValueError, KeyError, IndexError, OSError, pd.errors.EmptyDataError):
        return None


def resolve_quick_price(symbol: str, prices: dict, holdings: list, trades: pd.DataFrame):
    """不联网的价格兜底链，返回 (价格, 来源) 或 (None, None)。

    顺序：本地行情缓存 → 手动现价 → 持仓现价 → 最近一次成交价。
    """
    cached = cached_history_price(symbol)
    if cached is not None:
        return cached, "本地行情缓存"

    if symbol in prices and prices[symbol] > 0:
        return prices[symbol], "手动现价"

    for holding in holdings:
        if holding["symbol"] == symbol:
            return holding["price"], "持仓现价"

    # 最近一次成交价：从交易记录倒着找
    if not trades.empty:
        rows = trades[trades["股票代码"].str.strip().str.upper() == symbol]
        if not rows.empty:
            try:
                value = float(rows.iloc[-1]["价格"])
                if value > 0:
                    return value, "最近成交价"
            except (TypeError, ValueError):
                pass
    return None, None


# ---------------------------------------------------------------------------
# 历史行情与净值曲线
# ---------------------------------------------------------------------------
def _cache_file(symbol: str) -> str:
    return os.path.join(PRICE_CACHE_DIR, f"{symbol}.csv")


def _fail_marker(symbol: str) -> str:
    return os.path.join(PRICE_CACHE_DIR, f"{symbol}.fail")


def load_close_series(symbol: str, start: date, end: date):
    """取某只股票的历史日收盘价。

    返回 (Series, None) 表示成功；(None, 原因) 表示失败。
    优先读本地缓存（当天有效），避免每次刷新都打网络请求。
    """
    os.makedirs(PRICE_CACHE_DIR, exist_ok=True)
    cache = _cache_file(symbol)
    marker = _fail_marker(symbol)

    if os.path.exists(cache):
        try:
            cached = pd.read_csv(cache, parse_dates=["date"])
            is_fresh = date.fromtimestamp(os.path.getmtime(cache)) == date.today()
            covers = not cached.empty and cached["date"].min().date() <= start
            if is_fresh and covers:
                return cached.set_index("date")["close"], None
        except (ValueError, KeyError, pd.errors.EmptyDataError):
            pass                      # 缓存坏了就重新抓

    if ak is None:
        return None, "未安装 akshare"

    # 刚失败过就先别打网络：重复请求只会让限流更久
    if os.path.exists(marker):
        try:
            with open(marker, encoding="utf-8") as f:
                last_try = datetime.fromisoformat(f.read().strip())
            if datetime.now() - last_try < FAIL_BACKOFF:
                remain = int((FAIL_BACKOFF - (datetime.now() - last_try)).total_seconds() // 60) + 1
                return None, f"刚请求失败，冷却中（约 {remain} 分钟后可重试）"
        except (ValueError, OSError):
            pass

    try:
        raw = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",                # 不复权，和券商 App 里看到的收盘价一致
        )
    except Exception as exc:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except OSError:
            pass
        text = str(exc)
        if "限流" in text or "RateLimit" in type(exc).__name__:
            return None, "东方财富接口限流，请过几分钟点「重新获取行情」"
        return None, f"请求失败（{type(exc).__name__}）"

    if raw is None or raw.empty or "收盘" not in raw.columns:
        return None, "接口没有返回这只股票的数据，请确认代码是否正确"

    series = pd.Series(
        pd.to_numeric(raw["收盘"], errors="coerce").values,
        index=pd.to_datetime(raw["日期"]),
    ).dropna()
    if series.empty:
        return None, "返回的数据为空"

    series.rename("close").rename_axis("date").reset_index().to_csv(cache, index=False)
    if os.path.exists(marker):
        os.remove(marker)             # 成功了就把失败标记清掉
    return series, None


@st.cache_data(ttl=1800, show_spinner=False)
def build_price_frame(symbols: tuple, start: date, end: date, fallback: dict):
    """构建「每天 × 每只股票」的收盘价矩阵。

    - 自然日对齐后用前向填充：周末、节假日自动沿用上一个交易日的收盘价，
      所以曲线不会断，也不会出现缺口。
    - 某只股票完全取不到数据时，用它最近一次成交价当常数，并记录原因。
    """
    days = pd.date_range(start, end, freq="D")
    frame = pd.DataFrame(index=days)
    failed = []
    problems = {}

    for index, symbol in enumerate(symbols):
        if index:
            time.sleep(0.4)           # 多只股票时错开请求，降低被限流的概率
        series, error = load_close_series(symbol, start, end)
        if series is None or series.empty:
            failed.append(symbol)
            problems[symbol] = error or "未知原因"
            frame[symbol] = float(fallback.get(symbol, 0.0))
            continue
        # 对齐到每个自然日 → 前向填充 → 开头若还有缺口用最早的收盘价补
        frame[symbol] = series.reindex(days).ffill().bfill()

    return frame, failed, problems


def build_nav_curve(trades: pd.DataFrame, close_df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """逐日回放交易，算每天的总资产 = 当日现金 + 当日持仓市值。"""
    days = pd.date_range(start, end, freq="D")

    # 按日期归集当天发生的所有交易
    by_day = defaultdict(list)
    for _, row in trades.iterrows():
        try:
            day = pd.Timestamp(row["交易日期"]).normalize()
            symbol = str(row["股票代码"]).strip().upper()
            qty = int(row["数量"])
            price = float(row["价格"])
            fee = float(row["手续费"] or 0)
            side = row["买卖方向"]
        except (TypeError, ValueError):
            continue
        if not symbol or qty <= 0 or price <= 0:
            continue
        by_day[day].append((symbol, side, qty, price, fee))

    cash = INITIAL_CAPITAL
    positions = defaultdict(int)
    records = []

    for day in days:
        for symbol, side, qty, price, fee in by_day.get(day, []):
            amount = price * qty
            if side == "买入":
                cash -= amount + fee
                positions[symbol] += qty
            else:
                cash += amount - fee
                positions[symbol] -= qty

        market_value = 0.0
        for symbol, qty in positions.items():
            if qty <= 0:
                continue
            market_value += qty * float(close_df.at[day, symbol])

        records.append({
            "日期": day,
            "总资产": cash + market_value,
            "现金": cash,
            "持仓市值": market_value,
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# 绩效指标（胜率 / 最大回撤 / 夏普比率 / 收益回撤比）
# ---------------------------------------------------------------------------
RISK_FREE_RATE = 0.02         # 无风险利率，按一年 2% 算
TRADING_DAYS = 252            # 一年约 252 个交易日


def closed_trade_stats(trades: pd.DataFrame) -> dict:
    """统计「平仓交易」的胜率。

    口径说明（和持仓成本保持一致，避免两处数字打架）：
    - 每一次「卖出」算一笔平仓交易；
    - 赚没赚，用移动加权平均成本判断（成本含买入手续费，卖出手续费也扣掉）；
    - 卖出数量超过持仓的部分不计入（正常流程下不会出现）。
    """
    positions = {}            # 代码 -> [股数, 成本价]
    results = []              # 每笔平仓的盈亏

    for _, row in trades.iterrows():
        try:
            symbol = str(row["股票代码"]).strip().upper()
            qty = int(row["数量"])
            price = float(row["价格"])
            fee = float(row["手续费"] or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or qty <= 0 or price <= 0:
            continue

        pos = positions.setdefault(symbol, [0, 0.0])
        if row["买卖方向"] == "买入":
            amount = price * qty
            new_qty = pos[0] + qty
            pos[1] = (pos[1] * pos[0] + amount + fee) / new_qty
            pos[0] = new_qty
        else:
            sold = min(qty, pos[0])
            if sold > 0:
                results.append((price - pos[1]) * sold - fee)
            pos[0] -= qty
            if pos[0] <= 0:
                pos[0] = 0
                pos[1] = 0.0

    wins = sum(1 for pnl in results if pnl > 0)
    total = len(results)
    return {
        "rounds": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": (wins / total) if total else None,
    }


def max_drawdown(nav: pd.DataFrame) -> float:
    """净值从历史最高点跌到最低点的最大跌幅，返回正数（0.12 表示跌了 12%）。"""
    if nav.empty:
        return 0.0
    values = nav["总资产"].astype(float)
    peak = values.cummax()
    # max(0, ...) 是为了防住 -0.0：浮点减法会得到负零，页面会显示成「-0.00%」
    return max(0.0, -float((values / peak - 1).min()))


def sharpe_ratio(nav: pd.DataFrame, risk_free: float = RISK_FREE_RATE):
    """夏普比率 = 每承担一单位风险，能获得多少超额回报。

    用每日收益率算，再年化（乘 √252）。无风险利率按 2% 折算到每天。
    数据不足、或净值完全没有波动时返回 None —— 硬算出来是 0 或无穷大，对新手没意义。
    """
    if len(nav) < 3:
        return None
    values = nav["总资产"].astype(float)
    returns = values.pct_change().dropna()
    if returns.empty or len(returns) < 2:
        return None
    std = returns.std(ddof=1)
    if not std or std <= 0 or pd.isna(std):
        return None
    excess = returns - risk_free / TRADING_DAYS
    return float(excess.mean() / std * (TRADING_DAYS ** 0.5))


def calmar_ratio(total_return: float, max_dd: float):
    """收益回撤比 = 总收益率 / 最大回撤，衡量「赚的钱值不值得担的险」。"""
    if max_dd <= 0:
        return None
    return total_return / max_dd


def render_metric_card(col, label: str, value: str, note: str, help_text: str = None) -> None:
    """指标卡 + 数值下方一行大白话说明。

    st.metric 只支持 hover 的 help，新手指望不上；所以在卡片下面直接挂一行小字。
    """
    with col:
        st.metric(label, value, help=help_text)
        st.markdown(f'<div class="metric-note">{note}</div>', unsafe_allow_html=True)


def run_health_checks(stats: dict, max_dd: float, sharpe, calmar, total_trades: int) -> list:
    """账户体检：根据交易数据自动生成大白话建议。

    返回 [(级别, 文案), ...]，级别 'warning' 用 st.warning 展示，
    'info' 用 st.info 展示。指标算不出来时跳过对应规则，不误报。
    """
    tips = []

    # 赢小亏大：经常赚钱但赚得少、亏起来伤筋动骨
    if (
        stats["win_rate"] is not None
        and stats["win_rate"] > 0.6
        and calmar is not None
        and calmar < 1
    ):
        tips.append(("warning", "你虽然经常赚钱，但每次赚得很少；一旦亏钱，亏得很多。这叫赢小亏大！"))

    # 回撤过大：账户缩水超过五分之一
    if max_dd > 0.20:
        tips.append(("warning", "注意仓位控制！最惨的时候你的账户缩水了超过五分之一，新手一定要设好止损。"))

    # 夏普为负：担了风险没换来回报
    if sharpe is not None and sharpe < 0:
        tips.append(("warning", "你承担了很高的波动风险，但并没有赚到钱，这笔交易不划算。"))

    # 交易太频繁：手续费侵蚀利润
    if total_trades > 30:
        tips.append(("warning", "交易太频繁啦！频繁买卖交的手续费会吃掉你的利润。"))

    # 一条都没触发也要给正反馈，不能让新手以为体检坏了
    if not tips:
        tips.append(("info", "体检通过 ✅ 目前没发现明显问题，继续保持，也别忘了常回来看看这四个指标的变化。"))

    return tips


# ---------------------------------------------------------------------------
# 新手情绪感知
# ---------------------------------------------------------------------------
def emotion_banner(total: float):
    """奶茶计价器：把账户盈亏换算成吃的喝的，放在页面最顶端。

    返回 (级别, 文案)：'error' 亏损警告 / 'success' 盈利庆祝 / 'info' 风平浪静。
    ±0.005 元以内视为不赚不亏，避免显示「亏损了 0.00 元」这种废话。
    """
    pnl = total - INITIAL_CAPITAL
    if pnl < -0.005:
        cups = max(1, round(-pnl / 15))       # 瑞幸 15 元/杯
        meals = max(1, round(-pnl / 20))      # 食堂鸡腿饭 20 元/顿
        return ("error", (
            f"警告：你目前亏损了 **{abs(pnl):,.2f}** 元，"
            f"这相当于你少喝了 **{cups}** 杯瑞幸咖啡（按15元/杯算），"
            f"或少吃了 **{meals}** 顿食堂鸡腿饭（按20元/顿算）！"
        ))
    if pnl > 0.005:
        feasts = max(1, round(pnl / 100))     # 大餐 100 元/顿
        cups = max(1, round(pnl / 15))
        return ("success", (
            f"干得漂亮！目前赚了 **{pnl:,.2f}** 元，"
            f"够搓 **{feasts}** 顿大餐（按100元/顿算），"
            f"或请自己喝 **{cups}** 杯奶茶犒劳一下！"
        ))
    return ("info", "😐 目前不赚不亏，账户风平浪静 —— 正是冷静观察市场的好时候。")


def personality_tags(trades: pd.DataFrame, account: dict, win_rate, calmar) -> list:
    """性格照妖镜：根据交易行为给用户打标签。返回 [(标签, 提示), ...]。

    - 多动症选手：买卖总次数 > 20
    - 赢小亏大选手：胜率 > 50% 且收益回撤比 < 1（指标算不出来时跳过，不误报）
    - 全仓梭哈勇士：单笔最大买入金额 ÷ 当前总资产 > 80%
    """
    tags = []

    if len(trades) > 20:
        tags.append(("多动症选手", "交易太频繁啦！手续费正在疯狂吸你的血。"))

    if win_rate is not None and win_rate > 0.5 and calmar is not None and calmar < 1:
        tags.append(("赢小亏大选手", "赚了一点就跑，亏了死扛，这是新手最容易被割韭菜的姿势。"))

    max_buy = 0.0
    for _, row in trades.iterrows():
        if row["买卖方向"] == "买入":
            try:
                max_buy = max(max_buy, float(row["价格"]) * int(row["数量"]))
            except (TypeError, ValueError):
                pass
    total = account["total"]
    if max_buy > 0 and total > 0 and max_buy / total > 0.8:
        tags.append(("全仓梭哈勇士", "永远不要满仓！给自己留点补仓和后路的机会。"))

    return tags


# ---------------------------------------------------------------------------
# 平行宇宙沙盒
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 新手名词翻译官
# ---------------------------------------------------------------------------
TERM_DICT = {
    "市盈率": (
        "市盈率 = 股价 ÷ 每年赚的钱。相当于你买一个下金蛋的鸡，"
        "花了 20 倍于它一年下蛋收入的钱。数字越低说明「回本越快」，"
        "但太高也可能说明大家都很看好它——就像网红奶茶店排队越长，"
        "不代表奶茶更好喝，只代表更贵。"
    ),
    "最大回撤": (
        "最大回撤 = 你的账户从最高点摔到最低点的最大跌幅。"
        "就像坐过山车：爬到 100 米最高点，然后吓到 80 米，"
        "那你的「心理阴影面积」就是 20%。这个数字衡量的是「最惨的时候有多惨」，"
        "用来判断你晚上睡不睡得着觉。"
    ),
    "做多": (
        "做多 = 普通人买股票的方式：10 块买进，15 块卖出，赚差价。"
        "赌的是「它会涨」。你平时在炒股软件里的每一次买入，都是在做多。"
        "反面教材：做多失败俗称「站岗」——买在山顶上，风吹日晒等解套。"
    ),
    "做空": (
        "做空 = 反着来：先借别人的股票高价卖掉，等跌了再低价买回来还给他。"
        "赌的是「它会跌」。就像你借朋友一辆车 10 万卖掉，"
        "等车降价 8 万再买一辆还他，白赚 2 万。"
        "⚠️ A股普通散户基本玩不了这个，知道概念就行。"
    ),
    "割肉": (
        "割肉 = 亏本卖出股票。就像你花 100 块买了块肉，"
        "坏了只能 50 块卖掉，心里在滴血，但不卖会臭在手里。"
        "关键不是要不要割，而是别等到肉已经臭到 5 块钱都没人要才割——"
        "那叫「深度套牢」，是新手亏大钱的主要原因。"
    ),
    "追高": (
        "追高 = 看到股票猛涨就忍不住冲进去买。"
        "就像看到奶茶店排队 200 米，你立刻加入队伍——"
        "结果排到你的时候店宣布今天卖完了，或者你买到手发现就那样。"
        "暴涨之后往往紧跟回调，追高的人经常买在短期最高点，"
        "俗称「给别人抬轿子」。"
    ),
    "仓位": (
        "仓位 = 你有多少钱在股市里。全部投入叫「满仓」，"
        "一半叫「半仓」，一分没买叫「空仓」，只买两成叫「轻仓」。"
        "就像打食堂：把饭卡里所有钱一次全打红烧肉叫满仓——"
        "万一今天的红烧肉难吃，你连换个菜的余地都没有。"
        "新手建议永远别满仓。"
    ),
    "牛市": (
        "牛市 = 大盘持续上涨、大家都赚钱的时期，人人都是股神，"
        "小区保安都在聊 K 线。就像学校食堂涨价前的抢饭窗口——"
        "什么位置都能挤进去赚一口。"
        "⚠️ 危险在于：牛市里赚的钱容易让人误以为是自己的本事，"
        "其实是潮水把所有船都抬起来了。"
    ),
    "熊市": (
        "熊市 = 大盘持续下跌、大家都在亏钱的时期，"
        "股民群里一片哀嚎，谁提股票跟谁急。"
        "熊市才是真正学东西的时候：牛市赚钱靠运气，"
        "熊市少亏钱靠本事。记住巴菲特那句话：别人恐惧时贪婪——"
        "但新手第一课是：别人恐惧时，你先别乱动。"
    ),
    "涨停": (
        "涨停 = A股一天最多涨 10%（主板），涨到上限就锁死价格。"
        "就像食堂红烧肉限量 10 份，卖完立刻关窗——"
        "后面排再长的队今天也买不到。涨停说明想买的人挤破头，"
        "但第二天是继续冲还是「开板」回落，谁也说不准。"
    ),
    "跌停": (
        "跌停 = 一天最多跌 10%（主板），跌到下限就锁死价格。"
        "像股票被按在地上摩擦：想卖的人排成长队，"
        "但窗口根本没人接。连续跌停是最惨的剧本——"
        "想割肉都割不出去，只能眼睁睁看它每天再缩 10%。"
    ),
    "K线": (
        "K线 = 一根蜡烛记录一天的故事：开盘价、收盘价、当天最高最低。"
        "A股习惯红蜡烛 = 涨，绿蜡烛 = 跌。"
        "像天气预报里的温度曲线：一眼看出这天是阳光普照还是狂风暴雨。"
        "一堆K线连起来，就是这只股票的「心电图」。"
    ),
    "成交量": (
        "成交量 = 一天里有多少股换了主人，代表市场的热闹程度。"
        "涨价 + 放量（量大），说明是真金白银在追捧；"
        "涨价但量很小，就要小心是不是「虚火」。"
        "就像奶茶店：排队 200 米是真好喝，柜台前没几个人却宣称爆款，多半有问题。"
    ),
    "换手率": (
        "换手率 = 一天里有多大比例的流通股换了主人。"
        "换手率高说明筹码大挪移、多空分歧激烈。"
        "就像一个班一天之内换了一半座位——肯定发生什么大事了，"
        "至于是好消息还是坏消息，得结合涨跌来看。"
    ),
    "停牌": (
        "停牌 = 股票临时「禁赛」，暂停交易。"
        "可能是重大消息要宣布，也可能是被交易所请去「喝茶」（问询）。"
        "停牌期间你的钱被锁在里面动不了，"
        "复牌可能涨停也可能跌停——相当于开盲盒，新手尽量别重仓停牌股。"
    ),
    "套牢": (
        "套牢 = 买完就跌，卖吧心疼，不卖继续跌——"
        "钱被「锁」在高高的山岗上。"
        "就像花 100 块买的演唱会门票，黄牛现在只出 60——"
        "卖不卖都难受。避免套牢的关键是买入前就想好：跌到多少我认错离场。"
    ),
    "解套": (
        "解套 = 套牢的股票终于涨回你的买入价。"
        "像蹲了很久终于刑满释放——很多人一解套立刻卖掉，"
        "其实更该问的是：它现在还值不值得持有？"
        "别忘了解套只是「回本」，这几年耽误的时间也是成本。"
    ),
    "补仓": (
        "补仓 = 跌了再买一批，摊低成本。"
        "10 块买 100 股，跌到 8 块再买 100 股，成本就变成 9 块。"
        "像衣服买贵了降价又买一件——前提是这件衣服真的好，"
        "而不是越买越多压满箱底。补仓救的是好股票，救不了烂公司。"
    ),
    "利好": (
        "利好 = 对股价有好处的事：业绩暴增、政策扶持、大订单。"
        "⚠️ 但记住「利好出尽是利空」——消息正式公布那天，"
        "往往是提前埋伏的人卖出、股价下跌的开始。"
        "利好要买在「还没人知道」的时候，等你知道基本就晚了。"
    ),
    "利空": (
        "利空 = 坏消息：业绩爆雷、被处罚、大股东减持。"
        "新手要点：利空落地不一定就反弹，但乱接飞刀一定疼——"
        "看到利空别急着「抄便宜」，先想想为什么它跌。"
        "有些利空是黄金坑，有些利空是地狱的门，新手分不出来就先别动。"
    ),
    "打新股": (
        "打新股 = 用股票市值参与抽签买新股，A股传统里中了基本算白捡。"
        "像演唱会门票抽签——中签率极低（万分之一到百分之几），"
        "可以顺手打，但别指望靠这个发财，更不要为了打新专门去凑市值。"
    ),
    "蓝筹股": (
        "蓝筹股 = 大公司、业绩稳、经常分红的股票，比如银行、白酒、家电龙头。"
        "像班里成绩稳定的好学生：不会给你惊喜，"
        "也基本不给你惊吓。适合求稳的人，想快速发财的会嫌它慢。"
    ),
    "主力": (
        "主力 = 资金量大、能影响股价的大户或机构，俗称「庄家」。"
        "像食堂里一次打 1000 份饭的团体——他们一动，"
        "窗口队伍立刻变样。散户看到的数据往往是主力想让你看到的，"
        "所以别迷信「跟着主力吃肉」，你看到的可能是诱饵。"
    ),
    "T+1": (
        "T+1 = 今天买的股票，明天才能卖，A 股的规矩。"
        "像食堂打的饭不能当场退。这条规则防的就是新手一天之内"
        "反复横跳——但注意：今天卖掉的钱当天还能再买，只是买的新股票明天才能卖。"
    ),
    "分红": (
        "分红 = 公司赚了钱，拿一部分分给股东（发现金或送股票）。"
        "但注意除权除息：分红当天股价会相应下调，"
        "比如 10 块的股票分 0.5 块，开盘就变 9.5 块——"
        "分红不是白捡的钱，长期看拼的还是公司真能赚钱。"
    ),
    "市净率": (
        "市净率 = 股价 ÷ 每股净资产，衡量你花几倍的钱买这家公司的「家底」。"
        "像二手市场买车：卖价比账面值高多少倍。"
        "低于 1 理论上是「打折卖资产」，但要先问："
        "为什么打折？是市场不识货，还是资产本身有问题？"
    ),
    "抄底": (
        "抄底 = 赌已经跌了很多的股票「跌到头了」，冲进去捡便宜。"
        "俗称徒手接飞刀：接住是英雄，接不住满手血。"
        "跌 30% 之后还可能再跌 30%——底只有一个点，"
        "新手猜中的概率约等于抽奖。真想买，分批买、买有基本面的。"
    ),
}

# 常见别称 -> 标准词条名，用户搜「PE」「爆仓」之类也能命中
TERM_ALIASES = {
    "pe": "市盈率",
    "pe值": "市盈率",
    "本益比": "市盈率",
    "回撤": "最大回撤",
    "清仓割肉": "割肉",
    "止损": "割肉",
    "涨停板": "涨停",
    "封板": "涨停",
    "跌停板": "跌停",
    "蜡烛图": "K线",
    "k线图": "K线",
    "量能": "成交量",
    "补仓": "补仓",
    "加仓": "补仓",
    "摊低成本": "补仓",
    "好消息": "利好",
    "利多": "利好",
    "坏消息": "利空",
    "申购新股": "打新股",
    "新股": "打新股",
    "白马股": "蓝筹股",
    "庄家": "主力",
    "机构": "主力",
    "大资金": "主力",
    "除权除息": "分红",
    "派息": "分红",
    "接飞刀": "抄底",
    "满仓": "仓位",
    "轻仓": "仓位",
    "半仓": "仓位",
    "建仓": "仓位",
    "多头": "做多",
    "空头": "做空",
    "大盘": "牛市",
    "熊出没": "熊市",
}


def translate_term(query: str):
    """在名词字典里查一个术语，返回 (标准词条, 解释) 或 None。

    匹配顺序：全等（含别称） -> 词条名包含查询 -> 查询包含词条名。
    查询为空或太短返回 None，避免搜索框一打开就乱命中。
    """
    if query is None:
        return None
    q = str(query).strip().lower()
    if not q:
        return None

    if q in TERM_ALIASES:
        term = TERM_ALIASES[q]
        return term, TERM_DICT[term]
    for name, desc in TERM_DICT.items():
        if q == name.lower():
            return name, desc

    # 模糊：词条名含查询词（如搜「盈率」命中「市盈率」）或查询含词条名（如搜「什么是市盈率」）
    for name, desc in TERM_DICT.items():
        if name in q or q in name.lower():
            return name, desc
    return None


def closed_rounds(trades: pd.DataFrame) -> list:
    """用 FIFO（先买先卖）把每笔卖出和它对应的买入批次配对。

    返回每个「平仓回合」的明细：
    - cost_amount：这笔卖出实际占用的本金（买入金额 + 分摊的手续费）
    - realized_pnl：真实盈亏（卖出所得 - 占用本金 - 分摊的手续费）
    - buy_date / sell_date：回合的起止日期（买入取最早被卖掉的那批）
    """
    lots = defaultdict(list)          # 代码 -> [[日期, 剩余股数, 买价, 每股手续费]]
    rounds = []

    for _, row in trades.iterrows():
        try:
            symbol = str(row["股票代码"]).strip().upper()
            day = pd.Timestamp(row["交易日期"]).normalize()
            qty = int(row["数量"])
            price = float(row["价格"])
            fee = float(row["手续费"] or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or qty <= 0 or price <= 0:
            continue

        if row["买卖方向"] == "买入":
            lots[symbol].append([day, qty, price, fee / qty])
            continue

        # 卖出：按 FIFO 消耗批次
        pool = lots[symbol]
        remaining = qty
        sell_fee_ps = fee / qty       # 卖出手续费按股分摊
        consumed = []                 # (股数, 买价, 买入每股手续费, 买入日期)
        while remaining > 0 and pool:
            lot = pool[0]
            take = min(remaining, lot[1])
            consumed.append((take, lot[2], lot[3], lot[0]))
            lot[1] -= take
            remaining -= take
            if lot[1] <= 0:
                pool.pop(0)
        if not consumed:
            continue                  # 无货可卖（正常流程不会出现），跳过

        cost = sum(q * (p + fps) for q, p, fps, _ in consumed)
        pnl = sum(q * (price - sell_fee_ps - p - fps) for q, p, fps, _ in consumed)
        rounds.append({
            "symbol": symbol,
            "sold_qty": qty,
            "cost_amount": cost,
            "realized_pnl": pnl,
            "buy_date": min(c[3] for c in consumed).date(),
            "sell_date": day.date(),
        })

    return rounds


# 大盘基准选项：label 用于下拉菜单，name 用于文案，sina 是新浪接口的指数代码
BENCHMARKS = [
    {"label": "沪深300（代表大盘蓝筹）", "name": "沪深300", "sina": "sh000300"},
    {"label": "上证指数（代表沪市整体）", "name": "上证指数", "sina": "sh000001"},
    {"label": "创业板指（代表科技成长）", "name": "创业板指", "sina": "sz399006"},
]
DEFAULT_BENCHMARK = BENCHMARKS[0]["label"]


def simulated_index_series(start: date, end: date) -> pd.Series:
    """指数抓不到时的模拟数据：假设大盘持平（每天收盘价恒为 1000）。

    故意做成「零涨跌」而不是编一个看似真实的走势——模拟数据只能保证
    沙盒能出结果，不能用来下结论，页面上会明确警告这一点。
    """
    days = pd.date_range(start, end, freq="D")
    return pd.Series(1000.0, index=days, name="close")


def load_index_series(start: date, end: date, sina_symbol: str = "sh000300"):
    """取指数在指定区间的日收盘价，返回 (Series, None) 或 (None, 原因)。

    用新浪指数接口（stock_zh_index_daily），一次拿全量历史再按区间裁剪，
    比东财个股接口抗造得多；同样走本地缓存 + 失败冷却。
    缓存和冷却标记按指数代码分开存，三个基准互不干扰。
    """
    os.makedirs(PRICE_CACHE_DIR, exist_ok=True)
    cache = os.path.join(PRICE_CACHE_DIR, f"index_{sina_symbol}.csv")
    marker = os.path.join(PRICE_CACHE_DIR, f"index_{sina_symbol}.fail")

    if os.path.exists(cache):
        try:
            cached = pd.read_csv(cache, parse_dates=["date"])
            is_fresh = date.fromtimestamp(os.path.getmtime(cache)) == date.today()
            covers = not cached.empty and cached["date"].min().date() <= start
            if is_fresh and covers:
                return cached.set_index("date")["close"], None
        except (ValueError, KeyError, pd.errors.EmptyDataError):
            pass

    if ak is None:
        return None, "未安装 akshare"

    if os.path.exists(marker):
        try:
            with open(marker, encoding="utf-8") as f:
                last_try = datetime.fromisoformat(f.read().strip())
            if datetime.now() - last_try < FAIL_BACKOFF:
                remain = int((FAIL_BACKOFF - (datetime.now() - last_try)).total_seconds() // 60) + 1
                return None, f"刚请求失败，冷却中（约 {remain} 分钟后可重试）"
        except (ValueError, OSError):
            pass

    try:
        raw = ak.stock_zh_index_daily(symbol=sina_symbol)
    except Exception as exc:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except OSError:
            pass
        return None, f"请求失败（{type(exc).__name__}）"

    if raw is None or raw.empty or "close" not in raw.columns:
        return None, "接口没有返回指数数据"

    series = pd.Series(
        pd.to_numeric(raw["close"], errors="coerce").values,
        index=pd.to_datetime(raw["date"]),
    ).dropna().sort_index()
    series.rename("close").rename_axis("date").reset_index().to_csv(cache, index=False)
    if os.path.exists(marker):
        os.remove(marker)

    window = series[(series.index.date >= start) & (series.index.date <= end)]
    if window.empty:
        return None, "区间内没有指数数据"
    return window, None


def parallel_universe(round_info: dict, index_series: pd.Series):
    """算「如果这笔钱买了沪深300」会变成多少。

    周末/节假日没有指数报价，沿用前一交易日收盘价（ffill），
    保证买入日、卖出日一定能取到价。指数数据缺失时返回 None。
    """
    if index_series is None or index_series.empty:
        return None

    days = pd.date_range(round_info["buy_date"], round_info["sell_date"], freq="D")
    window = index_series.reindex(days).ffill().bfill()
    if window.isna().any() or len(window) < 2:
        return None

    start_close = float(window.iloc[0])
    end_close = float(window.iloc[-1])
    if start_close <= 0:
        return None

    ret = end_close / start_close - 1
    parallel_value = round_info["cost_amount"] * (1 + ret)
    return {
        "index_ret": ret,
        "parallel_value": parallel_value,
        "index_pnl": parallel_value - round_info["cost_amount"],
    }


def make_nav_figure(nav: pd.DataFrame, first_day: date, today: date) -> "go.Figure":
    """画出账户净值折线图。

    数据点很少时（比如刚录入第一笔只有 1 天），把坐标轴前后各扩一段，
    否则 Plotly 会把日期轴放大到小时/毫秒级刻度，曲线缩成一个看不见的点。
    """
    span_days = (today - first_day).days
    pad_days = max(7, int(span_days * 0.1))
    few_points = len(nav) <= 31

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=nav["日期"],
            y=nav["总资产"],
            mode="lines+markers" if few_points else "lines",
            name="总资产",
            line=dict(color="#d62728", width=2.2),
            marker=dict(size=7, symbol="circle", color="#d62728"),
            hovertemplate="%{x|%Y-%m-%d}<br>总资产 %{y:,.2f} 元<extra></extra>",
        )
    )
    fig.add_hline(
        y=INITIAL_CAPITAL,
        line_dash="dash",
        line_color="#9aa0a6",
        line_width=1,
        annotation_text="初始资金 100,000",
        annotation_position="bottom right",
        annotation_font=dict(color="#6b7280", size=11),
    )
    fig.update_layout(
        title=dict(text="我的账户净值曲线", x=0.01, xanchor="left"),
        xaxis_title="日期",
        yaxis_title="总资产（元）",
        hovermode="x unified",
        template="plotly_white",
        height=430,
        margin=dict(l=10, r=20, t=60, b=10),
        showlegend=False,
    )
    fig.update_yaxes(tickformat=",.0f", gridcolor="#eef0f2")
    fig.update_xaxes(
        range=[first_day - timedelta(days=pad_days), today + timedelta(days=pad_days)],
        tickformat="%m-%d",
        showgrid=False,
    )
    return fig


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="TradeLens · 模拟炒股训练舱",
    page_icon="🎓",
    layout="wide",
)

# 指标卡下方大白话小字的样式：不写死颜色，用 inherit + opacity，深浅主题都能看清
st.markdown(
    """
    <style>
    .metric-note {
        font-size: 0.78rem;
        line-height: 1.45;
        opacity: 0.62;
        margin: -0.5rem 0 1rem 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if "form_seq" not in st.session_state:
    st.session_state.form_seq = 0
if "flash" not in st.session_state:
    st.session_state.flash = None

trades = load_trades()
prices = load_prices()
account = build_account(trades, prices)
holdings_by_symbol = {h["symbol"]: h for h in account["holdings"]}

# ---- 顶部标题 ----
st.title("🎓 TradeLens 模拟炒股训练舱")
st.caption("零基础也能上手：给你 10 万虚拟资金，随便练，不用花真钱。")

# ---- 新手体验区：一键载入经典教学案例 ----
with st.expander("💡 新手不知道怎么玩？点击体验经典教学案例"):
    st.caption(
        "点任意按钮，系统会用一个「典型新手亏钱剧本」替换当前账户"
        "（现有记录自动备份到 backups/，不会丢），"
        "然后往下看奶茶计价器、性格照妖镜和净值曲线会发生什么。"
    )
    case_a, case_b, case_c = st.columns(3)
    with case_a:
        st.markdown("**案例一：多动症短线客**\n\n10 笔频繁买卖，赚小亏小，手续费吃掉全部利润。")
        if st.button("【案例一：多动症短线客】", key="demo_hyper", width="stretch"):
            archived, summary = apply_demo_case("hyper")
            st.session_state.flash = (
                f"案例已载入：多动症短线客（{summary}）。"
                + (f"原有记录已备份（{', '.join(archived)}）。" if archived else "")
                + "现在去看下面的「性格照妖镜」——它会给你贴什么标签？",
                "success",
            )
            st.rerun()
    with case_b:
        st.markdown("**案例二：全仓梭哈勇士**\n\n10 万本金硬买 13 万的茅台一手，跌了不卖还补仓。")
        if st.button("【案例二：全仓梭哈勇士】", key="demo_yolo", width="stretch"):
            archived, summary = apply_demo_case("yolo")
            st.session_state.flash = (
                f"案例已载入：全仓梭哈勇士（{summary}）。"
                + (f"原有记录已备份（{', '.join(archived)}）。" if archived else "")
                + "注意看顶部的可用现金——已经变成负数了。",
                "success",
            )
            st.rerun()
    with case_c:
        st.markdown("**案例三：追涨杀跌小白**\n\n大涨追高买、跌一点恐慌割肉、回本了又忍不住追。")
        if st.button("【案例三：追涨杀跌小白】", key="demo_panic", width="stretch"):
            archived, summary = apply_demo_case("panic")
            st.session_state.flash = (
                f"案例已载入：追涨杀跌小白（{summary}）。"
                + (f"原有记录已备份（{', '.join(archived)}）。" if archived else "")
                + "去「绩效诊断」看看这个案例的胜率和夏普比率。",
                "success",
            )
            st.rerun()

# ---- 上一次操作的结果提示 ----
if st.session_state.flash:
    message, level = st.session_state.flash
    (st.success if level == "success" else st.warning)(message)
    st.session_state.flash = None

# ---- 绩效指标提前算好（顶部情绪提示和净值曲线页共用，避免重复抓行情）----
perf = None
if not trades.empty:
    perf_symbols = tuple(sorted({
        str(s).strip().upper()
        for s in trades["股票代码"].dropna()
        if str(s).strip()
    }))
    perf_first_day = pd.to_datetime(trades["交易日期"]).min().date()
    perf_today = date.today()
    perf_fetch_start = perf_first_day - timedelta(days=10)   # 多取几天，保证首日也有前收盘价

    # 取不到行情时的兜底价：优先用手动现价，其次用最近一次成交价
    perf_fallback = {}
    for _, row in trades.iterrows():
        try:
            perf_fallback[str(row["股票代码"]).strip().upper()] = float(row["价格"])
        except (TypeError, ValueError):
            pass
    for holding in account["holdings"]:
        perf_fallback[holding["symbol"]] = holding["price"]

    with st.spinner("正在获取历史收盘价，第一次会慢一些…"):
        close_df, fetch_failed, fetch_problems = build_price_frame(
            perf_symbols, perf_fetch_start, perf_today, perf_fallback
        )

    nav = build_nav_curve(trades, close_df, perf_first_day, perf_today)
    perf_final_value = float(nav["总资产"].iloc[-1])
    perf_total_return = perf_final_value / INITIAL_CAPITAL - 1
    perf_dd = max_drawdown(nav)

    perf = {
        "symbols": perf_symbols,
        "first_day": perf_first_day,
        "today": perf_today,
        "nav": nav,
        "failed": fetch_failed,
        "problems": fetch_problems,
        "final_value": perf_final_value,
        "total_return": perf_total_return,
        "dd": perf_dd,
        "sharpe": sharpe_ratio(nav),
        "calmar": calmar_ratio(perf_total_return, perf_dd),
        "stats": closed_trade_stats(trades),
    }

# ---- 新手情绪感知：奶茶计价器（页面最顶端）----
banner_level, banner_text = emotion_banner(account["total"])
if banner_level == "error":
    st.error(banner_text, icon="⚠️")     # markdown 加粗数字；alert 本身就是大号红框
elif banner_level == "success":
    st.success(banner_text, icon="🎉")
else:
    st.info(banner_text, icon="😐")

# ---- 性格照妖镜 ----
if perf is not None:
    tags = personality_tags(trades, account, perf["stats"]["win_rate"], perf["calmar"])
    st.markdown("#### 🔮 性格照妖镜")
    if tags:
        tag_cols = st.columns(len(tags))
        for tag_col, (tag, tip) in zip(tag_cols, tags):
            with tag_col:
                st.error(f"**【{tag}】**\n\n{tip}", icon="🎭")
    else:
        st.info("目前没有明显的性格标签 —— 稳扎稳打型选手 👍", icon="👍")

# ---- 资金池指标 ----
m1, m2, m3, m4 = st.columns(4)
m1.metric(
    "总资产（元）",
    f"{account['total']:,.2f}",
    delta=f"{account['total_pnl']:+,.2f}",
    delta_color="inverse",        # 中国习惯：涨红跌绿
    help="总资产 = 可用现金 + 持仓市值",
)
m2.metric("可用现金（元）", f"{account['cash']:,.2f}", help="还能拿来买股票的钱")
m3.metric("持仓市值（元）", f"{account['market_value']:,.2f}", help="按当前价算，你手里股票值多少钱")
m4.metric(
    "累计盈亏（元）",
    f"{account['total_pnl']:+,.2f}",
    delta=f"{account['total_pct']:+.2%}",
    delta_color="inverse",
    help="相对初始 10 万，你总共赚了或亏了多少",
)

if account["cash"] < 0:
    st.warning(
        "⚠️ **可用现金是负数**：已有记录的花费超过了 10 万初始资金，"
        "大概率是之前的测试数据（例如那条 16.8 万的茅台买入）。\n\n"
        "在左侧边栏点「重置并重新开始」即可从头练起 —— 旧记录会自动备份到 `backups/` 文件夹，不会丢。"
    )

st.divider()

# ---- 侧边栏 ----
with st.sidebar:
    st.header("⚙️ 账户设置")
    st.markdown(
        f"""
- 初始虚拟资金：**{INITIAL_CAPITAL:,.0f} 元**
- 手续费：按你填的金额扣除
- A股规则：买入需 100 股整数倍
- 现价：由你手动调整，用来模拟涨跌
"""
    )
    st.divider()
    st.subheader("🔄 重置账户")
    st.caption("会清空当前交易记录，并自动备份到 `backups/` 文件夹。")
    if st.checkbox("我确认要重置", key="chk_reset"):
        if st.button("重置并重新开始", key="btn_reset", width="stretch"):
            archived = reset_account()
            st.session_state.flash = (
                "账户已重置，虚拟资金恢复为 100,000 元。"
                + (f"（旧记录已备份：{', '.join(archived)}）" if archived else ""),
                "success",
            )
            st.session_state.form_seq += 1
            st.rerun()
    else:
        st.button("重置并重新开始", key="btn_reset_disabled", disabled=True, width="stretch")

# ---- 功能页 ----
tab_trade, tab_pos, tab_curve, tab_log, tab_dict = st.tabs(
    ["📝 交易录入", "📊 持仓与盈亏", "📈 净值曲线", "📜 交易流水", "📖 名词翻译官"]
)

# =====================================================================
# 交易录入
# =====================================================================
with tab_trade:
    # ================== 新手快捷下单 ==================
    with st.container(border=True):
        st.markdown("#### ⚡ 新手快捷下单")
        st.caption(
            "不想填表单？选股票 → 定数量 → 点按钮。价格自动抓实时行情，"
            "手续费按万分之三自动算（不足 5 元按 5 元收）。"
        )

        qs1, qs2 = st.columns([3, 1])
        with qs1:
            quick_choice = st.selectbox(
                "选一只经典股票",
                list(QUICK_STOCKS.keys()),
                key="quick_stock",
                help="茅台 = 大盘蓝筹代表；平安银行 = 银行股；宁德时代 = 新能源成长股。",
            )
        with qs2:
            quick_qty = st.number_input(
                "数量（股）",
                min_value=100,
                value=100,
                step=100,
                format="%d",
                key="quick_qty",
                help="A股最少买 100 股（1 手），步长也是 100。",
            )

        # 下单前的参考信息（只读本地缓存，不打网络，页面不卡）
        quick_symbol = QUICK_STOCKS[quick_choice]
        quick_name = quick_choice.split(" ")[0]
        est_price, est_source = resolve_quick_price(
            quick_symbol, prices, account["holdings"], trades
        )
        if est_price is not None:
            est_fee = compute_quick_fee(est_price * quick_qty)
            est_amount = est_price * quick_qty
            if est_source == "本地行情缓存":
                src_text = "本地行情缓存"
            else:
                src_text = est_source
            st.caption(
                f"📌 参考价 **{est_price:,.2f} 元**（来源：{src_text}）· "
                f"买入 {quick_qty} 股约需 **{est_amount + est_fee:,.2f} 元**，"
                f"手续费约 {est_fee:,.2f} 元。点按钮时会先尝试抓实时价。"
            )
        else:
            st.caption(
                "📌 本地没有这只股票的价格记录，下单时会尝试抓实时行情；"
                "抓不到就用模拟价格成交（会明确提示）。"
            )

        qbtn1, qbtn2, _ = st.columns([1, 1, 2])
        quick_buy = qbtn1.button("🟢 一键买入", key="btn_quick_buy", width="stretch")
        quick_sell = qbtn2.button("🔴 一键卖出", key="btn_quick_sell", width="stretch")

        if quick_buy or quick_sell:
            quick_dir = "买入" if quick_buy else "卖出"
            try:
                live_price, live_source = fetch_live_price(quick_symbol)
            except Exception as exc:          # 双保险：函数内部已兜底，这里再兜一层
                live_price, live_source = None, f"实时行情异常（{type(exc).__name__}）"

            if live_price is not None:
                deal_price, price_source = live_price, live_source
            else:
                cached_price, cached_source = resolve_quick_price(
                    quick_symbol, prices, account["holdings"], trades
                )
                if cached_price is not None:
                    deal_price, price_source = cached_price, cached_source
                else:
                    deal_price, price_source = 100.00, "模拟价格"

            quick_fee = compute_quick_fee(deal_price * quick_qty)
            held_qty = holdings_by_symbol[quick_symbol]["qty"] if quick_symbol in holdings_by_symbol else 0
            errors = validate_trade(
                quick_symbol, quick_dir, deal_price, quick_qty, quick_fee,
                account["cash"], held_qty,
            )

            if errors:
                for msg in errors:
                    st.error(f"❌ {msg}")
            else:
                try:
                    append_trade({
                        "股票代码": quick_symbol,
                        "买卖方向": quick_dir,
                        "交易日期": date.today().strftime("%Y-%m-%d"),
                        "价格": round(deal_price, 4),
                        "数量": int(quick_qty),
                        "手续费": quick_fee,
                        "成交金额": round(deal_price * quick_qty, 2),
                    })
                    # 把成交价写进现价表，持仓页的市值联动用真实价
                    new_prices = dict(prices)
                    new_prices[quick_symbol] = round(deal_price, 4)
                    save_prices(new_prices)
                    if price_source == "模拟价格":
                        st.session_state.flash = (
                            f"下单成功！已以 **模拟价格 {deal_price:,.2f} 元**{quick_dir} "
                            f"{int(quick_qty)} 股 {quick_name}（{quick_symbol}）——"
                            "实时行情没抓到，价格仅供练习，手续费 "
                            f"{quick_fee:,.2f} 元。",
                            "success",
                        )
                    elif price_source != "实时行情":
                        st.session_state.flash = (
                            f"下单成功！已以 **{price_source} {deal_price:,.2f} 元**{quick_dir} "
                            f"{int(quick_qty)} 股 {quick_name}（{quick_symbol}），手续费 "
                            f"{quick_fee:,.2f} 元。",
                            "success",
                        )
                    else:
                        st.session_state.flash = (
                            f"下单成功！已以 {deal_price:,.2f} 元{quick_dir} "
                            f"{int(quick_qty)} 股 {quick_name}（{quick_symbol}），手续费 "
                            f"{quick_fee:,.2f} 元。",
                            "success",
                        )
                    st.rerun()
                except OSError as exc:
                    st.error(f"❌ 保存失败，文件写入出错：{exc}")

    st.divider()

    st.subheader("记录一笔模拟交易")
    st.info(
        "**新手须知**\n"
        "- A股数量必须是 **100 的整数倍**（100 股 = 1 手）\n"
        "- **买入和卖出都要付手续费**，别漏填\n"
        "- 买入花的钱不能超过你的可用现金"
    )

    seq = st.session_state.form_seq
    left, right = st.columns(2)

    with left:
        symbol = st.text_input(
            "股票代码",
            key=f"f_symbol_{seq}",
            placeholder="例如 600519",
            max_chars=16,
            help="填 6 位 A 股代码。600519 = 贵州茅台，000001 = 平安银行。",
        )
        direction = st.selectbox(
            "买卖方向",
            DIRECTIONS,
            key=f"f_dir_{seq}",
            help="买入 = 花钱买股票；卖出 = 把股票卖掉换回现金。",
        )
        trade_date = st.date_input(
            "交易日期",
            value=date.today(),
            key=f"f_date_{seq}",
            help="这笔交易发生的日期，默认今天。",
        )

    with right:
        price = st.number_input(
            "价格（元/股）",
            min_value=0.0,
            value=0.0,
            step=0.01,
            format="%.2f",
            key=f"f_price_{seq}",
            help="你的成交价。模拟盘不会自动抓实时行情，按你的判断填。",
        )
        quantity = st.number_input(
            "数量（股）",
            min_value=0,
            value=0,
            step=LOT_SIZE,
            format="%d",
            key=f"f_qty_{seq}",
            help="A股数量必须是 100 的整数倍：100、200、500……不能填 150。",
        )
        fee = st.number_input(
            "手续费（元）",
            min_value=0.0,
            value=0.0,
            step=0.01,
            format="%.2f",
            key=f"f_fee_{seq}",
            help="买卖都要手续费（佣金等），一般最低 5 元左右。可以先按 5 元估。",
        )

    # ---- 实时预览这笔交易的资金变化 ----
    clean_symbol = symbol.strip().upper()
    held = holdings_by_symbol.get(clean_symbol)
    if held:
        st.caption(
            f"📌 你当前持有 **{clean_symbol}** {held['qty']} 股，成本价 {held['cost']:.2f} 元。"
        )

    amount = price * quantity
    if price > 0 and quantity > 0:
        if direction == "买入":
            after = account["cash"] - amount - fee
            st.info(
                f"这笔**要花** {amount:,.2f} 元（含手续费 {fee:,.2f}）"
                f" → 成交后可用现金约 **{after:,.2f}** 元"
            )
        else:
            after = account["cash"] + amount - fee
            st.info(
                f"这笔**能收** {amount:,.2f} 元（扣手续费 {fee:,.2f}）"
                f" → 成交后可用现金约 **{after:,.2f}** 元"
            )

    if st.button("✅ 保存这笔交易", type="primary", width="stretch", key="btn_save"):
        held_qty = held["qty"] if held else 0
        errors = validate_trade(
            symbol, direction, price, quantity, fee, account["cash"], held_qty
        )
        if errors:
            for msg in errors:
                st.error(f"❌ {msg}")
        else:
            record = {
                "股票代码": clean_symbol,
                "买卖方向": direction,
                "交易日期": trade_date.strftime("%Y-%m-%d"),
                "价格": round(price, 4),
                "数量": int(quantity),
                "手续费": round(fee, 2),
                "成交金额": round(amount, 2),
            }
            append_trade(record)
            st.session_state.flash = (
                f"已保存：{record['股票代码']} {record['买卖方向']} "
                f"{record['数量']} 股 @ {record['价格']:.2f}，"
                f"成交金额 {record['成交金额']:,.2f} 元。",
                "success",
            )
            st.session_state.form_seq += 1     # 清空表单，避免重复提交
            st.rerun()

# =====================================================================
# 持仓与盈亏
# =====================================================================
with tab_pos:
    if not account["holdings"]:
        st.info("还没有持仓。先去「交易录入」买你的第一只股票吧。")
    else:
        # ---- 现价调整（模拟股价涨跌）----
        with st.expander("💹 调整现价（模拟股价涨跌，看看盈亏怎么变）", expanded=False):
            st.caption(
                "这里填的是「现在这只股票值多少钱」。改完点保存，上面的盈亏会跟着变。"
            )
            new_prices = {}
            cols = st.columns(3)
            for i, h in enumerate(account["holdings"]):
                with cols[i % 3]:
                    new_prices[h["symbol"]] = st.number_input(
                        f"{h['symbol']} 现价",
                        min_value=0.01,
                        value=float(h["price"]),
                        step=0.01,
                        format="%.2f",
                        key=f"px_{h['symbol']}",
                    )
            if st.button("保存现价", key="btn_save_prices"):
                merged = load_prices()
                merged.update(new_prices)
                save_prices(merged)
                st.session_state.flash = ("现价已更新，盈亏已重新计算。", "success")
                st.rerun()

        # ---- 持仓明细表 ----
        display = pd.DataFrame([
            {
                "股票代码": h["symbol"],
                "持股数": h["qty"],
                "成本价": round(h["cost"], 2),
                "现价": round(h["price"], 2),
                "市值": round(h["mv"], 2),
                "浮动盈亏": round(h["pnl"], 2),
                "收益率": f"{h['pct']:+.2%}",
                "大白话": h["plain"],
            }
            for h in account["holdings"]
        ])
        st.dataframe(display, width="stretch", hide_index=True)

        # ---- 持仓合计，用人话总结 ----
        total_mv = account["market_value"]
        total_float_pnl = account["unrealized"]
        total_cost = total_mv - total_float_pnl
        float_pct = total_float_pnl / total_cost if total_cost > 0 else 0.0

        if total_float_pnl > 0:
            st.success(
                f"**持仓合计**：市值 {total_mv:,.2f} 元，浮盈 **{total_float_pnl:+,.2f}** 元"
                f"（{float_pct:+.2%}）—— {pnl_plain(total_float_pnl, float_pct)}"
            )
        elif total_float_pnl < 0:
            st.error(
                f"**持仓合计**：市值 {total_mv:,.2f} 元，浮亏 **{total_float_pnl:+,.2f}** 元"
                f"（{float_pct:+.2%}）—— {pnl_plain(total_float_pnl, float_pct)}"
            )
        else:
            st.info(f"**持仓合计**：市值 {total_mv:,.2f} 元，暂时不赚不亏。")

        if abs(account["realized"]) > 0.004:
            st.caption(f"另外，你已经卖出并兑现的盈亏是 {account['realized']:+,.2f} 元。")

        st.caption(
            "💡 现价默认取你最近一次成交价。想模拟股价上涨或下跌，"
            "展开上面的「调整现价」改一下就行。"
        )

# =====================================================================
# 净值曲线
# =====================================================================
with tab_curve:
    st.subheader("我的账户净值曲线")

    if perf is None:
        st.info("还没有交易记录，先录一笔再来看曲线吧。")
    else:
        _, right_col = st.columns([3, 1])
        with right_col:
            if st.button("🔄 重新获取行情", key="btn_refresh_prices", width="stretch"):
                build_price_frame.clear()
                # 同时清掉失败冷却标记，让用户能立刻重试
                if os.path.isdir(PRICE_CACHE_DIR):
                    for name in os.listdir(PRICE_CACHE_DIR):
                        if name.endswith(".fail"):
                            try:
                                os.remove(os.path.join(PRICE_CACHE_DIR, name))
                            except OSError:
                                pass
                st.rerun()

        if perf["failed"]:
            detail = "；".join(f"**{s}**：{perf['problems'].get(s, '未知原因')}" for s in perf["failed"])
            st.warning(f"这些股票没取到行情，已用最近成交价代替 —— {detail}。")

        k1, k2 = st.columns(2)
        k1.metric(
            "期末总资产（元）",
            f"{perf['final_value']:,.2f}",
            delta=f"{perf['final_value'] - INITIAL_CAPITAL:+,.2f}",
            delta_color="inverse",
        )
        k2.metric("区间收益率", f"{perf['total_return']:+.2%}")

        fig = make_nav_figure(perf["nav"], perf["first_day"], perf["today"])
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

        # ---------------- 绩效诊断 ----------------
        st.markdown("#### 🩺 绩效诊断")
        st.caption("这四个数字不看懂也没关系，每个下面都写了大白话。")

        stats = perf["stats"]
        dd = perf["dd"]
        sharpe = perf["sharpe"]
        calmar = perf["calmar"]

        # 胜率
        if stats["win_rate"] is None:
            win_value = "—"
            win_note = "你还没卖出过任何一笔，等有平仓记录了才有胜率。"
        else:
            win_value = f"{stats['win_rate']:.0%}"
            if stats["win_rate"] > 0.5:
                win_note = f"你卖出的 {stats['rounds']} 笔里，{stats['wins']} 笔赚钱，赢多输少。"
            elif stats["win_rate"] == 0.5:
                win_note = f"你卖出的 {stats['rounds']} 笔里，赚的和亏的一样多，刚好一半一半。"
            else:
                win_note = f"你卖出的 {stats['rounds']} 笔里，只有 {stats['wins']} 笔赚钱，输多赢少。"

        # 最大回撤
        dd_value = f"{dd:.2%}"
        if dd <= 0:
            dd_note = "账户还没经历过下跌，最高点就是现在，说明暂时没怎么亏过。"
        else:
            dd_note = f"最惨的时候，你的账户从最高点缩水了 {dd:.1%}。"

        # 夏普比率
        if sharpe is None:
            sharpe_value = "—"
            sharpe_note = "交易天数太少，还看不出「冒的险值不值」，多记几天再来。"
        else:
            sharpe_value = f"{sharpe:.2f}"
            if sharpe >= 1:
                sharpe_note = f"你每担惊受怕 1 次，赚回了 {sharpe:.1f} 次的回报，很划算。"
            elif sharpe >= 0:
                sharpe_note = "你担的风险比赚到的回报多，收益还有点不够劲。"
            else:
                sharpe_note = "扣掉 2% 的银行利息后，你担的波动没换来额外回报，不如存银行。"

        # 收益回撤比
        if calmar is None:
            calmar_value = "—"
            calmar_note = "账户还没出现过回撤，这个比值暂时算不出来。"
        else:
            calmar_value = f"{calmar:.2f}"
            if calmar < 0:
                calmar_note = "这段时间整体是亏的，冒了险却没换来收益。"
            elif calmar >= 1:
                calmar_note = f"你赚的钱，是最惨那次亏损的 {calmar:.1f} 倍，赚得还算稳。"
            else:
                calmar_note = f"你赚的钱只有最大回撤的 {calmar:.1f} 倍，为了这点收益冒的险有点大。"

        c1, c2, c3, c4 = st.columns(4)
        render_metric_card(c1, "胜率", win_value, win_note,
                           help_text="赚钱的平仓笔数 ÷ 总平仓笔数。每卖出一次算一笔平仓。")
        render_metric_card(c2, "最大回撤", dd_value, dd_note,
                           help_text="净值从历史最高点跌到最低点的最大跌幅，越小越稳。")
        render_metric_card(c3, "夏普比率", sharpe_value, sharpe_note,
                           help_text="每承担一单位风险能获得多少超额回报，无风险利率按 2% 算。大于 1 算不错。")
        render_metric_card(c4, "收益回撤比", calmar_value, calmar_note,
                           help_text="总收益率 ÷ 最大回撤。大于 1 说明赚的比最惨时亏的多。")

        # ---------------- 账户体检诊断 ----------------
        st.markdown("#### 💡 账户体检诊断")
        st.caption("根据你的交易数据自动生成，条条都有对应的指标依据。")
        for level, text in run_health_checks(stats, dd, sharpe, calmar, len(trades)):
            (st.warning if level == "warning" else st.info)(text)

        st.caption(
            "口径说明：胜率按「每卖出一次」算一笔平仓，用加权平均成本判断赚亏；"
            f"夏普比率用每日收益率年化计算（×√252），无风险利率 {RISK_FREE_RATE:.0%}。"
        )

        st.caption(
            f"📌 曲线覆盖 **{perf['first_day']} ~ {perf['today']}**，用每日收盘价计算。"
            "周末和节假日沿用前一交易日收盘价，所以曲线是连续的。\n"
            "页面顶部的持仓市值用的是你设置的「现价」，口径不同，两者数值可能略有差异。"
        )

# =====================================================================
# 交易流水
# =====================================================================
with tab_log:
    if trades.empty:
        st.info("还没有任何交易记录。")
    else:
        show = trades.iloc[::-1].reset_index(drop=True)   # 最新的排最上面
        st.dataframe(show, width="stretch", hide_index=True)
        st.caption(f"共 {len(trades)} 笔交易 · 数据文件：`{TRADES_FILE}`")

# =====================================================================
# 新手名词翻译官
# =====================================================================
with tab_dict:
    st.subheader("📖 新手名词翻译官")
    st.caption(
        "听不懂大家在说什么？输入一个术语，我用大白话给你翻译。"
        f"目前收录 {len(TERM_DICT)} 个词，欢迎搜索试试。"
    )

    query = st.text_input(
        "输入想查的术语",
        key="dict_query",
        placeholder="比如：割肉、市盈率、追高……",
        help="不用打全，搜「盈率」也能找到「市盈率」。",
    )

    result = translate_term(query)
    if result is None:
        if query and str(query).strip():
            known = "、".join(TERM_DICT)
            st.warning(
                f"「{query.strip()}」这个词我还没学会翻译 🥲 目前能翻译的有：{known}。"
            )
    else:
        term, desc = result
        st.info(f"**「{term}」**\n\n{desc}")

    # 顺手把全部词条列出来，零基础用户可以直接点着看，不用先知道名词
    with st.expander(f"📖 看看全部 {len(TERM_DICT)} 个词条"):
        for name, desc in TERM_DICT.items():
            st.markdown(f"**{name}**\n\n{desc}")
            st.divider()

# =====================================================================
# 平行宇宙沙盒（页面底部）
# =====================================================================
st.divider()
st.subheader("🌌 平行宇宙沙盒")
st.caption(
    "每当你卖出一只股票，我们偷偷派另一个「你」去买指数基金——"
    "看看不用脑子选股的那个你，过得怎么样。"
)
st.info(
    "💡 **新手提示**：基准对比是专业投资的第一步——"
    "买小盘股多看创业板指，买大盘股多看沪深300。"
)

sand_rounds = closed_rounds(trades) if not trades.empty else []
if not sand_rounds:
    st.info("还没有卖出过任何一笔。等你平仓后，平行宇宙的大门才会打开。")
else:
    bench_label = st.selectbox(
        "选择要对比的大盘基准",
        options=[b["label"] for b in BENCHMARKS],
        index=0,
        key="bench_choice",
        help="沪深300 = 大盘蓝筹；上证指数 = 沪市整体；创业板指 = 科技成长。",
    )
    bench = next(b for b in BENCHMARKS if b["label"] == bench_label)

    index_start = min(r["buy_date"] for r in sand_rounds)
    index_end = max(r["sell_date"] for r in sand_rounds)
    index_series, index_error = load_index_series(
        index_start - timedelta(days=7),
        index_end + timedelta(days=1),
        sina_symbol=bench["sina"],
    )

    # 容错：抓不到就用模拟数据（假设大盘持平），并明确警告结论仅供参考
    using_simulated = False
    if index_series is None:
        index_series = simulated_index_series(index_start, index_end)
        using_simulated = True
        reason = index_error or "未知原因"
        st.warning(
            f"⚠️ **{bench['name']}行情没抓到（{reason}），已用模拟数据代替"
            "（假设大盘持平 0%）。下面的对比结论仅供参考，等行情恢复后再看真实结果。**"
        )

    for r in sand_rounds:
        with st.container(border=True):
            st.write(
                f"**📤 现实世界**：你在 {r['buy_date']} ~ {r['sell_date']} 期间持有 "
                f"**{r['symbol']}** {r['sold_qty']} 股，投入本金 "
                f"**{r['cost_amount']:,.2f} 元**，这笔操作真实盈亏 "
                f"**{r['realized_pnl']:+,.2f} 元**。"
            )

            result = parallel_universe(r, index_series)
            if result is None:
                reason = index_error or "指数数据不足"
                st.write(f"🌌 平行宇宙暂时算不出来（{reason}）。过几分钟再回来看看。")
                continue

            if using_simulated:
                st.write(
                    f"**🌌 平行宇宙（模拟数据）**：{bench['name']}行情缺失，"
                    "按大盘持平计算，你这笔钱保持 "
                    f"**{result['parallel_value']:,.2f} 元**不变。真实结果等行情恢复后再看。"
                )
            else:
                st.write(
                    f"**🌌 平行宇宙**：如果你当初没有买这只股票，而是把钱买了"
                    f"{bench['name']}指数基金，你这笔钱会变成 "
                    f"**{result['parallel_value']:,.2f} 元**"
                    f"（同期{bench['name']} {result['index_ret']:+.2%}）。"
                )

            diff = r["realized_pnl"] - result["index_pnl"]
            if using_simulated:
                # 模拟数据下大盘盈亏恒为 0，结论就是真实盈亏的正负，标注清楚即可
                if diff > 0.005:
                    st.write("**🟢 模拟结论：大盘按持平算的话，你这笔操作是赚钱的。**")
                elif diff < -0.005:
                    st.write("**🔴 模拟结论：大盘按持平算的话，你这笔操作是亏钱的。**")
                else:
                    st.write("**🤝 模拟结论：这笔操作不赚不亏。**")
            elif diff > 0.005:
                st.write(
                    f"**✅ 结论：你这笔操作跑赢了{bench['name']} {diff:,.2f} 元。** "
                    "选股（或择时）确实带来了价值！"
                )
            elif diff < -0.005:
                st.write(
                    f"**💔 结论：你这笔操作跑输了{bench['name']} {-diff:,.2f} 元。** "
                    "有时候「躺平买指数」反而更省心。"
                )
            else:
                st.write(f"**🤝 结论：你这笔操作和{bench['name']}打了个平手。**")
