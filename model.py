# -*- coding: utf-8 -*-
"""结果表的纯数据逻辑：行的比对键、删除行后已粘贴标记的重新编号。不依赖界面。"""
import csv

from app_config import one_line


def row_key(data, cols):
    """一行的比对键，用于判断重复行。"""
    return tuple(one_line(data.get(c, "")).strip() for c in cols)


def reindex_done(done, sid, removed_idx):
    """同一截图删除了若干行（removed_idx 为删除前的行号）后，重算已粘贴标记 {(截图id, 行号, 列名)}。"""
    def new_index(i):
        return i - sum(1 for d in removed_idx if d < i)

    removed_set = set(removed_idx)
    return {
        (s, new_index(i), c) if s == sid else (s, i, c)
        for (s, i, c) in done
        if not (s == sid and i in removed_set)
    }


def export_csv(path, columns, rows):
    """把结果表写成 CSV。rows 为每行的 dict 列表。用 UTF-8 带 BOM 编码，Excel 直接打开中文不乱码。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for data in rows:
            w.writerow([data.get(c, "") for c in columns])
