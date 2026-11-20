"""路由漂移的回归测试。

历史事故：`TiShiNengSdkPublic` 改成 `route('listBasUserImageFace')`，但路由表没有这个名字，
运行时抛 `UnknownRouteError`（“更新人脸图片”功能必崩）。这里用 AST 扫全部源码，
保证「代码里引用的路由名」与「路由表里的键」永远一致。
"""
import ast
import pathlib
import unittest

from tsn import routes as R

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {'tests', '__pycache__', '.git', '.venv'}
SKIP_FILES = {'diag', 'stage'}
_ROUTE_CALLS = {'route', 'build_url'}


def _source_files():
    for path in ROOT.rglob('*.py'):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith(tuple(SKIP_FILES)):
            continue
        yield path


def _referenced_names():
    """收集 route('x') / build_url(base, 'x') 里的字面量路由名。"""
    found = {}
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:  # 语法错误的文件交给编译检查，不在这里报路由问题
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _ROUTE_CALLS:
                continue
            args = node.args
            target = args[0] if node.func.attr == 'route' else (args[1] if len(args) > 1 else None)
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                found.setdefault(target.value, set()).add(str(path.relative_to(ROOT)))
    return found


class RouteContract(unittest.TestCase):
    def test_every_referenced_name_exists(self):
        missing = {name: files for name, files in _referenced_names().items() if name not in R.ROUTE_NAMES}
        self.assertEqual(missing, {}, f'这些路由名在代码里被引用但路由表没有: {missing}')

    def test_every_name_maps_to_a_url(self):
        broken = {name: token for name, token in R.ROUTE_NAMES.items() if token not in R.ROUTE_BY_TOKEN}
        self.assertEqual(broken, {})

    def test_every_relative_url_is_a_path(self):
        for name, path in R.all_routes().items():
            if R.is_absolute(path):
                continue
            self.assertFalse(path.startswith('/'), f'{name} 的相对路径不应以 / 开头')
            self.assertNotIn('//', path, f'{name} 的路径出现双斜杠')


if __name__ == '__main__':
    unittest.main()
