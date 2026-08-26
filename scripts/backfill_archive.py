#!/usr/bin/env python3
"""把历史 attempt 的 transcript 从 /tmp 抢救进审计目录。

## 为什么要这个脚本

归档功能上线之前跑过的 attempt，`transcript_path` 一律指向
`/tmp/factory-transcript-*`。那些文件**现在还在**（实测 1201 个目录），但
systemd-tmpfiles 会在 10 天后清掉，机器重启如果 /tmp 是 tmpfs 就当场清零。

跑一次这个脚本，把还能救的都搬进 `<data_root>/attempts/`，并把库里的路径
改成新位置。救不回来的（源已经没了）原样留着 —— 不改成空，那个失效路径本身
就是「现场曾经存在过、后来丢了」的证据。

## diff 救不回来

历史 attempt 只存了 `diff_hash`，正文当年就丢在内存里了。这个脚本只搬
transcript。老轮次的 diff 永久缺失，前端会显示「归档前的老 attempt」。

## 用法

    python scripts/backfill_archive.py --db ~/factory-data/audit.db
    python scripts/backfill_archive.py --db ~/factory-data/audit.db --dry-run

默认 dry-run 关闭 —— 这个操作是纯增量的（复制文件 + 改路径字段），
没有破坏性，不需要拿 dry-run 当保险。给 --dry-run 是为了先看一眼数量。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factory.audit.archive import ARCHIVE_DIRNAME, archive_attempt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path, help="audit.db 路径")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不动文件和库")
    args = ap.parse_args()

    db: Path = args.db.expanduser().resolve()
    if not db.exists():
        print(f"审计库不存在：{db}", file=sys.stderr)
        return 1

    data_root = db.parent
    print(f"审计库   : {db}")
    print(f"归档根   : {data_root / ARCHIVE_DIRNAME}")
    print(f"模式     : {'dry-run（不写任何东西）' if args.dry_run else '实际执行'}")
    print()

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, task_id, attempt_no, transcript_path FROM task_attempt "
        "WHERE transcript_path IS NOT NULL AND transcript_path != '' "
        "ORDER BY id"
    ).fetchall()

    already, rescued, lost = 0, 0, 0
    archive_prefix = str(data_root / ARCHIVE_DIRNAME)

    for aid, task_id, no, path in rows:
        # 已经在归档目录里的跳过 —— 脚本要能反复跑（幂等）。
        if path.startswith(archive_prefix):
            already += 1
            continue

        if not Path(path).exists():
            lost += 1
            print(f"  ✗ #{aid} {task_id}/{no} 源已丢失：{path}")
            continue

        if args.dry_run:
            rescued += 1
            print(f"  → #{aid} {task_id}/{no} 可救回（{Path(path).stat().st_size:,} 字节）")
            continue

        got = archive_attempt(data_root, task_id, no, transcript_src=path)
        if got.transcript_path:
            conn.execute(
                "UPDATE task_attempt SET transcript_path=? WHERE id=?",
                (got.transcript_path, aid),
            )
            rescued += 1
            print(f"  ✓ #{aid} {task_id}/{no} → {got.transcript_path}")
        else:
            lost += 1
            print(f"  ✗ #{aid} {task_id}/{no} 归档失败：{got.error}")

    if not args.dry_run:
        conn.commit()
    conn.close()

    print()
    print(f"总计 {len(rows)} 条带路径的 attempt：")
    print(f"  已在归档目录 : {already}")
    print(f"  本次救回     : {rescued}")
    print(f"  源已丢失     : {lost}")
    if lost:
        print()
        print("丢失的那些现场永久没了 —— 库里保留失效路径作为「曾经存在」的证据，")
        print("前端会显示「归档前的老 attempt，现场已随 /tmp 清理丢失」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
