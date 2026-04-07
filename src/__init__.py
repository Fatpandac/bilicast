# -*- coding: utf-8 -*-
import logging

logging.basicConfig(
    level="INFO",
)
log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())
