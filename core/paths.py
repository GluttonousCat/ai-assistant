"""Project and report-storage paths shared by configuration, logging, and jobs."""
import os
from pathlib import Path
from threading import Lock


def project_root() -> Path:
    """Locate the deployment's config next to the source, independent of cwd."""
    source_root = Path(__file__).resolve().parents[1]
    for directory in (source_root, *source_root.parents):
        if (directory / "config.yaml").is_file():
            return directory
    return source_root


class PathManager:
    """Keep historical per-group SQLite and download locations stable."""

    _instance = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                root = project_root()
                output = root / "output"
                output.mkdir(parents=True, exist_ok=True)
                instance.root_dir = str(root)
                instance.output_dir = str(output)
                cls._instance = instance
            return cls._instance

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
