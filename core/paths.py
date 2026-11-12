#!/usr/bin/env python3
"""
Path management module
Consolidated from the former db_path_manager.py
"""
import os


class PathManager:
    """Path manager"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True

        # Project root (walk upward for the directory containing config.yaml)
        self.root_dir = self._find_project_root()

        # Output directory
        self.output_dir = os.path.join(self.root_dir, "output")
        os.makedirs(self.output_dir, exist_ok=True)

    def _find_project_root(self) -> str:
        """Find the project root directory"""
        current = os.path.abspath(os.path.dirname(__file__))

        # Walk upward looking for config.yaml
        while True:
            if os.path.exists(os.path.join(current, "config.yaml")):
                return current

            parent = os.path.dirname(current)
            if parent == current:
                # Not found: fall back to this file's directory
                return os.path.abspath(os.path.dirname(__file__))

            current = parent

    def get_group_dir(self, group_id: str) -> str:
        """Get the group directory"""
        group_dir = os.path.join(self.output_dir, str(group_id))
        os.makedirs(group_dir, exist_ok=True)
        return group_dir

    def get_topics_db_path(self, group_id: str) -> str:
        """Get the topics database path"""
        group_dir = self.get_group_dir(group_id)
        return os.path.join(group_dir, f"topics_{group_id}.db")

    def get_files_db_path(self, group_id: str) -> str:
        """Get the files database path"""
        group_dir = self.get_group_dir(group_id)
        return os.path.join(group_dir, f"files_{group_id}.db")

    def get_download_dir(self, group_id: str) -> str:
        """Get the download directory"""
        group_dir = self.get_group_dir(group_id)
        download_dir = os.path.join(group_dir, "downloads")
        os.makedirs(download_dir, exist_ok=True)
        return download_dir
