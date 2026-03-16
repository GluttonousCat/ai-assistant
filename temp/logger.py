import logging
from logging.handlers import RotatingFileHandler
import yaml


def setup_logger(config):
    logger = logging.getLogger("WWD_System")
    logger.setLevel(config['logging']['level'])

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    file_handler = RotatingFileHandler(
        config['logging']['file'],maxBytes=5 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(formatter)

    # 控制台日志
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger