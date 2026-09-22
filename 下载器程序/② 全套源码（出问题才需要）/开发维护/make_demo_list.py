# -*- coding: utf-8 -*-
"""生成演示用的名单 Excel（纯虚构数据）。

刻意设计成三种情况混合，用来验证工具在真实场景下的表现：
  1. 16 人在演示站里存在        → 应判定「成功」，并抓到成绩
  2. 4 人不存在                 → 应判定「查无此人」
  3. 1 人用护照号               → 验证非身份证类证件也能处理
"""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

EXISTS = [
    ("演示学生01", "000000000000000001"), ("演示学生02", "000000000000000002"),
    ("演示学生03", "000000000000000003"), ("演示学生04", "000000000000000004"),
    ("演示学生05", "000000000000000005"), ("演示学生06", "000000000000000006"),
    ("演示学生07", "000000000000000007"), ("演示学生08", "000000000000000008"),
    ("演示学生09", "000000000000000009"), ("演示学生10", "000000000000000010"),
    ("演示学生11", "000000000000000011"), ("演示学生12", "000000000000000012"),
    ("演示学生13", "000000000000000013"), ("演示学生14", "000000000000000014"),
    ("演示学生15", "000000000000000015"), ("演示学生16", "000000000000000016"),
]
MISSING = [
    ("演示缺考01", "000000000000000101"),
    ("演示缺考02", "000000000000000102"),
    ("演示缺考03", "000000000000000103"),
    ("演示缺考04", "000000000000000104"),
    ("演示护照01", "G12345678"),          # 护照号（合成占位）
]

wb = Workbook()
ws = wb.active
ws.title = "参赛名单"
ws.append(["序号", "姓名", "证件类型", "证件号", "参赛项目"])
for i, (n, d) in enumerate(EXISTS + MISSING, 1):
    ws.append([i, n, "护照" if not d.isdigit() else "身份证", d, "青少年科技创新大赛"])

for c in ws[1]:
    c.font = Font(bold=True)
    c.alignment = Alignment(horizontal="center")
ws.column_dimensions["A"].width = 6
ws.column_dimensions["B"].width = 12
ws.column_dimensions["C"].width = 10
ws.column_dimensions["D"].width = 24
ws.column_dimensions["E"].width = 24

# 跟着脚本走，别写死绝对路径 —— 项目文件夹换个地方放就失效了
out = Path(__file__).resolve().parent / "名单示例_演示用.xlsx"
wb.save(str(out))
print("生成完成：%s（共 %d 条）" % (out, len(EXISTS) + len(MISSING)))
