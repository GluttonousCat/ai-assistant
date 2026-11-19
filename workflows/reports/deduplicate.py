"""Deduplicate report attachments while preserving each topic's independent documents."""

from storage.repositories.report_duplicates import merge


if __name__ == "__main__":
    print(merge())
