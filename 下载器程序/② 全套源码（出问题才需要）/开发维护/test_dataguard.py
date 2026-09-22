# -*- coding: utf-8 -*-
"""数据守卫测试：同名续跑 + 证件号归一化。

这两件事都属于"静默漏数据"——不报错、不崩溃，只是结果悄悄少了几个人。
所以必须用真造的脏数据验证，不能靠"看起来没问题"。
"""
import csv
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "程序"))
import cert_downloader as CD          # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [OK] %s" % name)
    else:
        fail += 1
        print("  [!!] %s   %s" % (name, extra))


print("=" * 68)
print("① 证件号归一化（不归一化 → 查不到 → 被当成「未获奖」）")
print("=" * 68)
id_cases = [
    ("420106199001011234", "420106199001011234", "标准写法：原样不动"),
    ("4201 0619 9001 0112 34", "420106199001011234", "中间带空格"),
    ("42010619900101123x", "42010619900101123X", "末位小写 x → 大写"),
    ("４２０１０６１９９００１０１１２３４", "420106199001011234", "全角数字"),
    ("\t420106199001011234\n", "420106199001011234", "首尾有空白"),
    ("", "", "空值"),
    (None, "", "None 值"),
]
for src, want, desc in id_cases:
    got = CD.normalize_id(src)
    check("normalize_id  %-16s %r → %r" % (desc, src, got), got == want, "期望 %r" % want)

name_cases = [
    ("张三", "张三", "标准写法：原样不动"),
    ("张 三", "张三", "姓名里有空格"),
    ("　张三", "张三", "全角空格"),
    ("John Smith", "John Smith", "外文名的空格要保留"),
    ("阿依古丽·买买提", "阿依古丽·买买提", "间隔符「·」不能动"),
]
for src, want, desc in name_cases:
    got = CD.normalize_name(src)
    check("normalize_name %-16s %r → %r" % (desc, src, got), got == want, "期望 %r" % want)

print()
print("=" * 68)
print("② 续跑索引：同校同名的人不能互相顶掉")
print("=" * 68)

tmp = Path(tempfile.mkdtemp(prefix="certdl_test_"))
folder = tmp / "武汉市硚口区崇仁路小学"
folder.mkdir(parents=True)
(folder / "张伟.pdf").write_bytes(b"%PDF-1.5" + b"x" * 3000)
(folder / "张伟_2.pdf").write_bytes(b"%PDF-1.5" + b"y" * 3000)
with io.open(tmp / "下载明细_20260101_000000.csv", "w",
             encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["序号", "姓名", "证件号", "状态", "保存路径", "KB"])
    w.writerow([1, "张伟", "420106199001011234", "成功", str(folder / "张伟.pdf"), 3.0])
    w.writerow([2, "张伟", "420106199001011999", "成功", str(folder / "张伟_2.pdf"), 3.0])

idx = CD.load_done_index(tmp)
check("索引里记下 2 个不同的人", len(idx) == 2, "实际 %d" % len(idx))
check("张伟（尾号 1234）已记录", ("张伟", "420106199001011234") in idx)
check("张伟（尾号 1999）已记录", ("张伟", "420106199001011999") in idx)
check("★ 两个同名的人指向不同文件",
      idx.get(("张伟", "420106199001011234")) != idx.get(("张伟", "420106199001011999")))

os.remove(folder / "张伟.pdf")
idx2 = CD.load_done_index(tmp)
check("文件被手动删掉后 → 不再算「已完成」，会重新下",
      ("张伟", "420106199001011234") not in idx2)
check("另一个人的记录不受影响", ("张伟", "420106199001011999") in idx2)

# 残file（上次下到一半）不能算完成
(tmp / "残.pdf").write_bytes(b"")
(tmp / "下载明细_20260101_000001.csv").write_text("", encoding="utf-8")
idx3 = CD.load_done_index(tmp)
check("空文件不会被当成已完成", len(idx3) == 1, "实际 %d" % len(idx3))

print()
print("=" * 68)
print("③ 文件名冲突处理")
print("=" * 68)
d = tmp / "冲突测试"
d.mkdir()
p1 = CD.unique_path(d, "李雷")
check("空目录 → 直接用 李雷.pdf", p1.name == "李雷.pdf", p1.name)
p1.write_bytes(b"%PDF-1.5" + b"z" * 3000)
p2 = CD.unique_path(d, "李雷")
check("已被占用 → 李雷_2.pdf", p2.name == "李雷_2.pdf", p2.name)
(d / "残.pdf").write_bytes(b"")
p3 = CD.unique_path(d, "残")
check("撞上残file → 直接覆盖，不生成 残_2.pdf", p3.name == "残.pdf", p3.name)

shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 68)
print("④ 故障手册在删掉 Mac 分支后仍能正常出文案")
print("=" * 68)
import error_help as EH          # noqa: E402
for kind in ("blocked", "proxy", "dns", "timeout", "pdf_stuck", "list_file", "unknown"):
    lines = EH.explain(kind, "测试现场信息", tag="测试")
    txt = "\n".join(lines)
    check("explain(%s) 有内容" % kind, len(lines) >= 5 and "怎么办" in txt,
          "行数=%d" % len(lines))
proxy_txt = "\n".join(EH.explain("proxy"))
check("代理故障给出的是 Windows 路径（「设置 → 网络和 Internet → 代理」）",
      "网络和 Internet" in proxy_txt)
check("文案里不再混入 macOS 菜单路径", "系统设置" not in proxy_txt)
res = EH.resume_steps("proxy", 10, 100)
check("中止恢复步骤能正常生成", len(res) >= 4)
adv = EH.finish_advice({"proxy"}, {"_done": 10, "_total": 100}, aborted=True)
check("收尾建议能正常生成", len(adv) >= 3)

print()
print("=" * 68)
print("结果：%d 项通过，%d 项失败" % (ok, fail))
print("=" * 68)
sys.exit(1 if fail else 0)
