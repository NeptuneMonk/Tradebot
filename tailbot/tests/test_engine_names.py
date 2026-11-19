import ast
import builtins
import pathlib


def test_engine_module_names_resolve():
    # The position monitor and account handler use these names. A missing import
    # raises NameError every tick, so stops, trails, and momentum never fire.
    from tailbot import engine
    for name in ("STABLES", "WSOL", "decode_bonding_curve", "token_account_amount"):
        assert hasattr(engine, name), name


def test_no_undefined_module_level_names_in_package():
    pkg = pathlib.Path(__file__).resolve().parents[1] / "tailbot"
    for path in pkg.glob("*.py"):
        tree = ast.parse(path.read_text())
        defined = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
                if hasattr(node, "args"):
                    defined |= {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
            elif isinstance(node, ast.Lambda):
                defined |= {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.alias):
                defined.add((node.asname or node.name).split(".")[0])
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = used - defined - {"__name__", "__file__"}
        assert not missing, f"{path.name}: {sorted(missing)}"
