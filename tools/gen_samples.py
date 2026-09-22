#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成手写风计件单生成器（Day0 精度门用）。

三档难度各 10 张：
  A 档 = 印刷体清晰单（基线）
  B 档 = 逐字符抖动（模拟手写）
  C 档 = B + 灰噪背景 + 整页微旋转（模拟手机拍照）
每张带真值 JSON（assets/samples/ground_truth.json）。全部数据合成，零真实痕迹。
"""
import json
import math
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
OUT = os.path.join(os.path.dirname(__file__), "..", "assets", "samples")

WORKSHOP = ["冲压车间", "钣金车间", "装配车间"]
PROCESS = ["冲压", "折弯", "焊接", "打磨", "喷漆", "装配", "裁剪", "攻丝"]
NAMES = ["王建国", "李秀兰", "张卫东", "刘志强", "陈小红", "赵国庆",
         "孙丽华", "周建军", "吴春梅", "郑海涛", "冯玉珍", "蒋大明"]
UNITS = ["件", "套", "只"]

# 固定随机种子=结果可复现
random.seed(20260919)


def jitter_text(draw, xy, text, font, fill, jitter):
    """逐字符抖动绘制，模拟手写不稳。"""
    x, y = xy
    for ch in text:
        dx = random.uniform(-jitter, jitter)
        dy = random.uniform(-jitter, jitter)
        size_var = font.size + random.randint(-2, 2)
        f = ImageFont.truetype(FONT, size_var)
        draw.text((x + dx, y + dy), ch, font=f, fill=fill)
        x += f.getbbox(ch)[2] - f.getbbox(ch)[0] + random.randint(1, 4)


def make_sheet(idx, tier):
    """tier: A/B/C。返回 (PIL.Image, ground_truth dict)。"""
    W, H = 900, 640
    img = Image.new("RGB", (W, H), (248, 246, 240) if tier != "C" else (235, 232, 224))
    draw = ImageDraw.Draw(img)
    if tier == "C":  # 灰噪
        for _ in range(2600):
            x, y = random.randrange(W), random.randrange(H)
            g = random.randint(150, 215)
            draw.point((x, y), fill=(g, g, g - 8))

    ink = (30, 30, 34)
    f_head = ImageFont.truetype(FONT, 30)
    f_body = ImageFont.truetype(FONT, 22)
    j = 0 if tier == "A" else 2

    gt = {
        "sheet_no": f"SJ-2026{random.randint(100, 999)}",
        "workshop": random.choice(WORKSHOP),
        "date": f"2026-09-{random.randint(1, 25):02d}",
        "rows": [],
    }
    n = random.randint(5, 7)
    used_names = random.sample(NAMES, n)
    rows = []
    for i in range(n):
        row = {
            "process": random.choice(PROCESS),
            "name": used_names[i],
            "qty": random.randint(40, 480),
            "unit_price": round(random.uniform(0.35, 3.2), 2),
            "defect": random.choice([0, 0, 0, random.randint(1, 6)]),
        }
        rows.append(row)
    gt["rows"] = rows
    gt["total_amount"] = round(sum(r["qty"] * r["unit_price"] for r in rows), 2)

    # 抬头
    jitter_text(draw, (60, 28), f"计件工票  {gt['sheet_no']}", f_head, ink, j)
    jitter_text(draw, (60, 80), f"车间：{gt['workshop']}    日期：{gt['date']}", f_body, ink, j)

    # 表格
    cols = [60, 250, 420, 560, 700, 840]
    headers = ["工序", "姓名", "数量", "单价", "废品"]
    ty = 130
    for c, h in zip(cols[:5], headers):
        jitter_text(draw, (c + 6, ty), h, f_body, ink, j)
    draw.line([(60, ty + 40), (870, ty + 40)], fill=ink, width=2)
    y = ty + 52
    for r in rows:
        vals = [r["process"], r["name"], str(r["qty"]),
                f"{r['unit_price']:.2f}", str(r["defect"])]
        for c, v in zip(cols[:5], vals):
            jitter_text(draw, (c + 6, y), v, f_body, ink, j)
        draw.line([(60, y + 38), (870, y + 38)], fill=(120, 120, 120), width=1)
        y += 44
    jitter_text(draw, (60, y + 10), f"合计：¥{gt['total_amount']:.2f}", f_body, ink, j)
    jitter_text(draw, (560, y + 10), "审核：＿＿＿", f_body, ink, j)

    if tier == "C":  # 整页微旋转
        img = img.rotate(random.uniform(-2.2, 2.2), expand=False,
                         fillcolor=(235, 232, 224))
    return img, gt


def main():
    os.makedirs(OUT, exist_ok=True)
    truth = {}
    for tier in ("A", "B", "C"):
        for i in range(1, 11):
            idx = ({"A": 0, "B": 100, "C": 200}[tier]) + i
            img, gt = make_sheet(idx, tier)
            name = f"sheet_{tier}_{i:02d}.png"
            img.save(os.path.join(OUT, name))
            truth[name] = {"tier": tier, **gt}
    with open(os.path.join(OUT, "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump(truth, f, ensure_ascii=False, indent=1)
    print(f"生成 {len(truth)} 张样张 → {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
