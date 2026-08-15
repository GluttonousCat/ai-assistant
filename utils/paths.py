#!/usr/bin/env python3
"""
路径管理模块
整合原 db_path_manager.py
"""
import os
from pathlib import Path


class PathManager:
    """路径管理器"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True

        # 项目根目录（向上查找包含config.toml的目录）
        self.root_dir = self._find_project_root()

        # 输出目录
        self.output_dir = os.path.join(self.root_dir, "output")
        os.makedirs(self.output_dir, exist_ok=True)

    def _find_project_root(self) -> str:
        """查找项目根目录"""
        current = os.path.abspath(os.path.dirname(__file__))

        # 向上查找config.yaml
        while True:
            if os.path.exists(os.path.join(current, "config.yaml")):
                return current

            parent = os.path.dirname(current)
            if parent == current:
                # 未找到，使用当前目录
                return os.path.abspath(os.path.dirname(__file__))

            current = parent

    def get_group_dir(self, group_id: str) -> str:
        """获取群组目录"""
        group_dir = os.path.join(self.output_dir, str(group_id))
        os.makedirs(group_dir, exist_ok=True)
        return group_dir

    def get_topics_db_path(self, group_id: str) -> str:
        """获取话题数据库路径"""
        group_dir = self.get_group_dir(group_id)
        return os.path.join(group_dir, f"topics_{group_id}.db")

    def get_files_db_path(self, group_id: str) -> str:
        """获取文件数据库路径"""
        group_dir = self.get_group_dir(group_id)
        return os.path.join(group_dir, f"files_{group_id}.db")

    def get_download_dir(self, group_id: str) -> str:
        """获取下载目录"""
        group_dir = self.get_group_dir(group_id)
        download_dir = os.path.join(group_dir, "downloads")
        os.makedirs(download_dir, exist_ok=True)
        return download_dir

    def get_cache_dir(self, group_id: str) -> str:
        """获取缓存目录"""
        group_dir = self.get_group_dir(group_id)
        cache_dir = os.path.join(group_dir, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir


# 便捷函数
def get_path_manager() -> PathManager:
    """获取路径管理器实例"""
    return PathManager()
