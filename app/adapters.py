"""车辆过磅模拟适配器。

真实场景对接地磅/称重系统；本地平台用确定性的模拟结果返回净重，
同一派工重复过磅结果稳定（便于测试与演示）。
"""
from __future__ import annotations

import hashlib


class WeighbridgeAdapter:
    """模拟地磅适配器。"""

    def weigh(self, dispatch_code: str, bag_count: int) -> dict:
        # 每袋淤泥基准 26~30kg，依派工编号做确定性抖动；另加每袋 1.2kg 袋重，
        # 留给主管“核减袋重/雨水”的修订场景。
        digest = hashlib.sha1(dispatch_code.encode("utf-8")).hexdigest()
        jitter = int(digest[:6], 16) % 40 / 10.0  # 0.0 ~ 3.9 kg
        per_bag = 26.0 + jitter
        net = round(bag_count * per_bag + bag_count * 1.2, 1)
        return {
            "adapter": "mock-weighbridge-v1",
            "dispatch_code": dispatch_code,
            "bag_count": bag_count,
            "gross_kg": round(net + 180.0, 1),       # 含车皮模拟毛重
            "tare_kg": 180.0,
            "net_kg": net,
            "stable": True,
        }


adapter = WeighbridgeAdapter()
