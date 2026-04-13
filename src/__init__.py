# -*- coding: utf-8 -*-
import logging

logging.basicConfig(
    level="INFO",
)

for _logger_name in ("httpx", "httpcore"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())
