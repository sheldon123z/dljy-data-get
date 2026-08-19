#!/usr/bin/env python3
"""零价掩码的回归测试。

这套逻辑在修补上游数据源的一个缺陷：电查查接口对**没有数据**的时段返回数字 `0.0`
而不是 null，采集脚本原样落盘，于是数据仓里两种 0 混在一起：

  · 真实零电价 —— 午间光伏过剩压出来的成交价，**必须保留**；
  · 采集缺失   —— 那天那一列压根没出来，被写成一片 0，**必须掩掉**。

掩错任一方向都会让 agent 说出错误结论：
少掩 → 假零价拉低均价、伪造价差；多掩 → 把真实的负电价/零价窗口抹掉，
而那正是储能套利最关心的时段。所以两个方向都要有测试守着。

跑法（不依赖 pytest，标准库即可）：
    python3 tests/test_zero_masking.py
"""

import importlib.util
import sys
import unittest
from pathlib import Path

_MCP = Path(__file__).resolve().parent.parent / "mcp" / "power_price_mcp.py"
_spec = importlib.util.spec_from_file_location("power_price_mcp", _MCP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def rows(*triples):
    """(时点, 日前, 实时) 三元组序列，时点用 '00:15' 这种字符串。"""
    return list(triples)


def full_day(da_fn, rt_fn, points=96):
    """造一天 96 点。da_fn/rt_fn 接收时点分钟数，返回该点的价。"""
    out = []
    for i in range(1, points + 1):
        minutes = i * 15
        slot = f"{minutes // 60:02d}:{minutes % 60:02d}"
        out.append((slot, da_fn(minutes), rt_fn(minutes)))
    return out


NOON = range(10 * 60 + 45, 13 * 60 + 1)      # 光伏大发窗口
EVENING = range(19 * 60, 21 * 60 + 1)         # 晚高峰


class MaskRealZeroPrices(unittest.TestCase):
    """真实零价必须活下来。"""

    def test_noon_solar_zeros_are_kept(self):
        # 午间一段 0，晚高峰有正常高价 —— 典型的光伏过剩，是真成交价
        day = full_day(
            da_fn=lambda mm: 0.0 if mm in NOON else 300.0,
            rt_fn=lambda mm: 0.0 if mm in NOON else 350.0,
        )
        out, masked = m._mask_fake_zeros(day)
        self.assertEqual(masked, 0, "午间光伏零价被误掩了")
        kept = [r for r in out if r[m.RT] == 0.0]
        self.assertTrue(kept, "真实零价应当保留")

    def test_negative_prices_untouched(self):
        day = full_day(
            da_fn=lambda mm: -50.0 if mm in NOON else 300.0,
            rt_fn=lambda mm: -80.0 if mm in NOON else 350.0,
        )
        out, masked = m._mask_fake_zeros(day)
        self.assertEqual(masked, 0)
        self.assertTrue(any(r[m.RT] == -80.0 for r in out), "负电价不该受影响")


class MaskCollectionGaps(unittest.TestCase):
    """采集缺失必须掩掉。"""

    def test_whole_column_zero_is_masked(self):
        # 整天日前全 0：日前是前一天就排好的完整曲线，不可能整天零
        day = full_day(da_fn=lambda mm: 0.0, rt_fn=lambda mm: 350.0)
        out, masked = m._mask_fake_zeros(day)
        self.assertEqual(masked, 96)
        self.assertTrue(all(r[m.DA] is None for r in out), "整列全零应判缺失")
        self.assertTrue(all(r[m.RT] == 350.0 for r in out), "另一列不该被牵连")

    def test_zero_tail_to_end_of_day_is_masked(self):
        # 从下午一直 0 到 24:00，跨过晚高峰 —— 采集断在中途
        day = full_day(
            da_fn=lambda mm: 300.0,
            rt_fn=lambda mm: 0.0 if mm >= 14 * 60 else 350.0,
        )
        out, masked = m._mask_fake_zeros(day)
        self.assertGreater(masked, 0)
        tail = [r for r in out if m._slot_min(r[m.TIME]) >= 14 * 60]
        self.assertTrue(all(r[m.RT] is None for r in tail), "跨晚高峰的零尾段应判缺失")

    def test_double_zero_covering_evening_peak_is_masked(self):
        # 日前实时同时为 0 且覆盖晚高峰 —— 两侧同时恰好 0.00 只可能是没数据
        day = full_day(
            da_fn=lambda mm: 0.0 if mm in EVENING else 300.0,
            rt_fn=lambda mm: 0.0 if mm in EVENING else 350.0,
        )
        out, masked = m._mask_fake_zeros(day)
        self.assertGreater(masked, 0)
        peak = [r for r in out if m._slot_min(r[m.TIME]) in EVENING]
        self.assertTrue(all(r[m.DA] is None and r[m.RT] is None for r in peak))


class ResidualZerosAfterOtherRules(unittest.TestCase):
    """规则三：前两条掩完后，剩下的点若全是 0，整列判缺失。

    这是 2026-08-19 修的真 bug。原样本：黑龙江 2026-08-07 掩剩 6 个点、
    日前全 0.0，于是 day_ahead_avg=0.00、real_time_avg=211.87，
    get_price_trend 报出 spread_avg=211.87 —— 一个凭空捏造的价差。
    """

    def test_residual_all_zero_column_is_masked(self):
        # 造还原现场：绝大多数点两列都缺（写成 0 且跨晚高峰，会被规则一/二掩掉），
        # 残留几个点里日前是 0、实时是真价
        day = []
        for i in range(1, 97):
            minutes = i * 15
            slot = f"{minutes // 60:02d}:{minutes % 60:02d}"
            if i <= 6:
                day.append((slot, 0.0, 200.0 + i))   # 残留：日前假零，实时真价
            else:
                day.append((slot, 0.0, 0.0))          # 双零且延伸到 24:00
        out, masked = m._mask_fake_zeros(day)

        da_left = [r[m.DA] for r in out if r[m.DA] is not None]
        self.assertEqual(da_left, [], "残留的全零日前列应被判缺失")

        rt_left = [r[m.RT] for r in out if r[m.RT] is not None]
        self.assertEqual(len(rt_left), 6, "实时的真实价格不能被牵连掩掉")

        # 关键断言：均价与价差必须是 None，而不是被假零算成 0.0 / 凭空的价差
        self.assertEqual(m._stats(out, m.DA).get("avg"), None)
        self.assertEqual(m._spread_avg(out), None)

    def test_single_zero_point_is_not_over_masked(self):
        # 只有一个点是 0，其余正常 —— 可能是真实地板价，不能掩
        day = full_day(
            da_fn=lambda mm: 0.0 if mm == 11 * 60 else 300.0,
            rt_fn=lambda mm: 350.0,
        )
        out, masked = m._mask_fake_zeros(day)
        self.assertEqual(masked, 0, "孤立的零点不该被规则三误伤")
        self.assertTrue(any(r[m.DA] == 0.0 for r in out))


class MaskOrderWithUnclearedPoints(unittest.TestCase):
    """直取路径要先掩未出清、再掩假零，顺序不能反。

    fetch_live_price 走的是 _mask_uncleared → _mask_fake_zeros。盘中查当天时，
    尚未出清的时点接口也返回 0；那些点必须先按"未出清"处理掉，
    否则会被假零规则连累，把当天**已经出清**的真实价格一起判成缺失。
    """

    def test_settled_prices_survive_after_uncleared_masking(self):
        today = "2026-08-19"
        # 前 24 点已出清（有真实价），其余未出清（接口返回 0）
        day = []
        for i in range(1, 97):
            minutes = i * 15
            slot = f"{minutes // 60:02d}:{minutes % 60:02d}"
            if i <= 24:
                day.append((slot, 300.0 + i, 350.0 + i))
            else:
                day.append((slot, 0.0, 0.0))

        rows, cleared_until, uncleared = m._mask_uncleared(day, today)
        rows, faked = m._mask_fake_zeros(rows)

        # 24 个已出清的真实价格必须一个不少地活下来
        real = [r[m.RT] for r in rows if r[m.RT] is not None and r[m.RT] > 0]
        self.assertEqual(len(real), 24, "已出清的真实价格被误掩了")
        self.assertGreater(uncleared, 0, "未出清的点应当被识别出来")

        stats = m._stats(rows, m.RT)
        self.assertIsNotNone(stats.get("avg"))
        self.assertGreater(stats["avg"], 0, "已出清部分的均价应当是正常值")

        # 注：_mask_uncleared 有 CLEARING_LAG_CAP（6h）宽限期，落在窗口内的 0
        # 既可能是未出清、也可能是真实零价，代码选择保留——宁可少掩也不误伤
        # 真实零价。所以这里不断言"非 None 点数恰好等于 24"，那会随当前时刻漂移。


class WarehouseInvariants(unittest.TestCase):
    """跑在真实数据仓上的不变量——回归的最后一道闸。"""

    @classmethod
    def setUpClass(cls):
        if not m.DETAIL.exists():
            raise unittest.SkipTest(f"数据仓不存在：{m.DETAIL}")
        cls.idx = m._load_detail()

    def test_no_column_averages_to_fake_zero(self):
        """没有任何区域日的某一列会算出 0.00 的均价。

        真实出清里，一整天的日前或实时均价恰好为 0 不可能发生；
        出现即说明有假零漏网，会连带伪造出价差。
        """
        offenders = []
        for (province, trade_date), day in self.idx.items():
            for col, name in ((m.DA, "日前"), (m.RT, "实时")):
                vals = [r[col] for r in day if r[col] is not None]
                if vals and all(v == 0.0 for v in vals):
                    offenders.append(f"{province} {trade_date} {name}列({len(vals)}点)")
        self.assertEqual(offenders, [], f"存在会算出假零均价的区域日：{offenders[:5]}")

    def test_real_zero_prices_still_present(self):
        """掩码不能把真实零价一并抹掉——那是储能套利最关心的窗口。"""
        zero_points = sum(
            1 for day in self.idx.values() for r in day
            if r[m.RT] == 0.0
        )
        self.assertGreater(zero_points, 0, "真实零电价被全掩掉了，说明规则过激")


if __name__ == "__main__":
    unittest.main(verbosity=2)
