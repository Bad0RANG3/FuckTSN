"""设备画像。

历史合格记录全部来自 iPhone 17 Pro / iOS 27.0.1 / App 3.6.814；
机型/系统版本突变会被服务端当作环境异常（详见 docs/逆向笔记.md）。
设 TSN_DEVICE=android 可回退到 Android 画像做对比实验。
"""
from __future__ import annotations

import os


class DeviceModel:

    def __init__(self):
        if os.environ.get('TSN_DEVICE', '').lower() == 'android':
            self.brand = 'Xiaomi'
            self.model = '25042PN24C'
            self.osver = '16'
            self.a_list = 'arm64-v8a'
            self.system = 'Android'
            self.app_version = '3.6.814'
        else:
            self.brand = 'iPhone'
            self.model = 'iPhone 17 Pro'
            self.osver = '27.0.1'
            self.a_list = 'arm64-v8a'
            self.system = 'iOS'
            self.app_version = '3.6.814'

    def as_dict(self) -> dict:
        return {'brand': self.brand, 'model': self.model, 'osver': self.osver,
                'a_list': self.a_list, 'system': self.system, 'app_version': self.app_version}


deviceModel = DeviceModel()
