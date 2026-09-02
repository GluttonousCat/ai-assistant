# -*- encoding: utf-8 -*-
"""一次性清理: 删除 mp3 等音频研报文件 (本地 + SQLite files 表 + PG report_meta)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AUDIO_EXTS = ('.mp3', '.m4a', '.wav', '.aac', '.flac')
DL_DIR = os.path.join("output", "51288148188224", "downloads")
FILES_DB = os.path.join("output", "51288148188224", "files_51288148188224.db")


def clean_local():
    n = 0
    if os.path.isdir(DL_DIR):
        for f in os.listdir(DL_DIR):
            if f.lower().endswith(AUDIO_EXTS):
                os.remove(os.path.join(DL_DIR, f))
                n += 1
    print(f"[1] 本地音频文件删除: {n} 个")
    return n


def clean_sqlite():
    from storage.sqlite.files import FilesDatabase
    if not os.path.exists(FILES_DB):
        print("[2] files 库不存在, 跳过")
        return
    db = FilesDatabase(FILES_DB)
    for ext in AUDIO_EXTS:
        db.cursor.execute("UPDATE files SET download_status='skipped_audio' "
                          "WHERE lower(name) LIKE ?", (f"%{ext}",))
    db.conn.commit()
    n = db.cursor.execute(
        "SELECT COUNT(*) FROM files WHERE download_status='skipped_audio'").fetchone()[0]
    print(f"[2] SQLite files 表标记 skipped_audio: {n} 条")
    db.close()


def clean_pg():
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT report_id, file_name FROM fin.report_meta "
            "WHERE lower(file_name) LIKE '%.mp3' OR lower(file_name) LIKE '%.m4a' "
            "OR lower(file_name) LIKE '%.wav' OR lower(file_name) LIKE '%.aac'")
        for r in rows:
            pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s", (r["report_id"],))
            pg.execute("DELETE FROM fin.report_meta WHERE report_id=%s", (r["report_id"],))
        print(f"[3] PG 音频研报记录删除: {len(rows)} 条")


if __name__ == "__main__":
    clean_local()
    clean_sqlite()
    clean_pg()
    print("清理完成")
